"""Optional automated re-login, for when the session dies at 3am.

WHY THIS IS OPT-IN AND OFF BY DEFAULT
-------------------------------------
Every other part of this project is built so that no code path ever reads your
password — you log in by hand, once, and the session is reused. That is a real
security property and it is worth keeping if you can.

The cost of keeping it: when a session expires overnight, the watcher keeps
detecting and alerting but cannot hold anything until you sit down and log in.
The paid services do not have this problem because they hold their customers'
credentials outright.

So this exists as a deliberate trade you can make: put your credentials in
.env, and the watcher will try to sign itself back in once per expiry. Enable
it with session.auto_relogin: true. Leave it off and nothing here ever runs.

WHAT IT WILL NOT DO
-------------------
* It never retries in a loop. One attempt per expiry, then it alerts you.
  Repeated failed logins are how accounts get locked, and a locked account
  costs you every shift, not one.
* It cannot answer an OTP. If Amazon challenges the login, it stops and tells
  you. A persistent profile usually avoids the challenge because the machine
  is already a trusted device — that is the whole reason the profile exists.
* It never types anything anywhere except the login form on the auth domain.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import otp_mail

log = logging.getLogger(__name__)

# CONFIRMED live 2026-08-17 against auth.hiring.amazon.com/#/login.
EMAIL_INPUT = "#login, [data-test-id='input-test-id-login']"
CONTINUE_BUTTON = "[data-test-id='button-continue']"
CONSENT_BUTTON = "[data-test-id='consentBtn']"

# CONFIRMED live: the challenge screen reads "Where should we send your
# verification code?" and offers a single [data-test-id='button-submit'].
# It is only ever clicked when a mailbox is configured to read the reply —
# otherwise this would just mail a code to an inbox nobody is watching.
SEND_CODE_BUTTON = "[data-test-id='button-submit']"
# CONFIRMED live 2026-08-18 by walking to the code screen: the field sits
# inside [data-test-id='input-test-id-confirmOtp'] as a plain input with
# maxlength=6 and no test-id of its own, and the button is verifyAccount.
#
# Also learned there: the code EXPIRES IN 3 MINUTES and resend is blocked for
# 55 seconds — so reading it from email has to be prompt, and a retry costs
# most of a minute.
CODE_INPUT = (
    "[data-test-id='input-test-id-confirmOtp'] input, "
    "[data-test-id='input-test-id-confirmOtp'], "
    "input[maxlength='6'], "
    "[data-test-id*='otp'] input, "
    "input[inputmode='numeric'], "
    "input[type='tel']"
)
VERIFY_BUTTON = "[data-test-id='button-test-id-verifyAccount']"

# The login form has a REQUIRED country selector, and skipping it fails in a
# way that looks like something else entirely: Continue simply never becomes
# clickable, and the first attempt died on a 20s click timeout rather than on
# anything to do with credentials. The error only appears after you try:
# "Please select the country where you registered your account."
COUNTRY_TOGGLE = "#country-toggle-button"
COUNTRY_OPTION = "li[role='option']"

# Which country to pick, from the site being watched.
COUNTRY_BY_HOST = {
    "hiring.amazon.ca": "Canada",
    "hiring.amazon.com": "United States",
}

# Step 2 is a 6-DIGIT PIN, not a password. Learned from the competing
# service's own onboarding bot, which asks its customers for exactly that:
#
#   "Step 2 of 2 - Type Your PIN / Type your 6-digit PIN of Hiring Account"
#
# That also confirms how those services never appear to log out: they hold the
# customer's email and PIN and can re-authenticate whenever they like.
#
# The screen itself could not be captured — reaching it needs a real email
# submitted to a real account — so the field is matched widest-net first, and
# the attempt reports honestly when nothing matches.
PIN_INPUT = (
    "[data-test-id='input-test-id-pin'], "
    "[data-test-id*='pin'] input, "
    "input[type='password'], "
    "input[inputmode='numeric'], "
    "#pin, #password"
)
# Some PIN screens are six single-character boxes rather than one field.
PIN_BOXES = "input[maxlength='1']"
SUBMIT_BUTTON = (
    "[data-test-id='button-signIn'], "
    "[data-test-id='button-login'], "
    "[data-test-id='button-continue'], "
    "button[type='submit']"
)

# Two kinds of challenge, and they must not be confused.
#
# An emailed code CAN be finished automatically, if a mailbox is configured.
EMAIL_CODE_MARKERS = (
    "verification code",
    "one-time password",
    "one time password",
    "we sent a code",
    "enter the code",
    "check your email",
)

# A CAPTCHA cannot, ever. Solving image challenges is not something this
# project does — it is the line between automating your own login and
# impersonating a human to a system that asked whether you were one.
#
# Keeping these separate matters practically too: routing a CAPTCHA into the
# email path would click a "send code" button that is not there, then sit for
# two minutes waiting on mail that was never sent.
HUMAN_ONLY_MARKERS = (
    "puzzle",
    "captcha",
    "solve this",
    "select all images",
    "are you a robot",
    "verify you are human",
)

OTP_MARKERS = EMAIL_CODE_MARKERS + HUMAN_ONLY_MARKERS
WRONG_CREDENTIAL_MARKERS = (
    "incorrect",
    "does not match",
    "not recognised",
    "not recognized",
    "try again",
)

OK = "ok"
OTP_REQUIRED = "otp_required"
CAPTCHA = "captcha"
BAD_CREDENTIALS = "bad_credentials"
UNKNOWN = "unknown"


def credentials() -> tuple[str, str] | None:
    """Email and 6-digit PIN, from the environment only.

    Never from config.yaml: that file is committed to git, .env is not, and
    .env is already where the Telegram token lives.
    """
    email = os.getenv("AMAZON_LOGIN_EMAIL", "").strip()
    # AMAZON_LOGIN_PASSWORD still works — it was the original name, before the
    # competitor's own signup bot revealed the credential is a 6-digit PIN.
    pin = (os.getenv("AMAZON_LOGIN_PIN") or os.getenv("AMAZON_LOGIN_PASSWORD") or "").strip()
    if not email or not pin:
        return None
    if not (pin.isdigit() and len(pin) == 6):
        # Not fatal — Amazon may vary this — but worth saying out loud, since
        # a wrong credential burns one of the very few attempts we allow.
        log.warning(
            "AMAZON_LOGIN_PIN is %d character(s) and %s all digits; Amazon "
            "Hiring uses a 6-digit PIN",
            len(pin), "not" if not pin.isdigit() else "is",
        )
    return email, pin


def _page_text(page: Any) -> str:
    try:
        return (page.inner_text("body") or "").lower()
    except Exception as exc:  # noqa: BLE001 - mid-navigation is normal
        log.debug("could not read login page text: %s", exc)
        return ""


def _needs_a_human(text: str) -> str | None:
    """Any challenge at all, of either kind."""
    for marker in OTP_MARKERS:
        if marker in text:
            return marker
    return None


def _is_captcha(text: str) -> str | None:
    """A challenge nothing here will answer.

    Solving image puzzles is not something this project does: it is the line
    between automating your own login and impersonating a human to a system
    that just asked whether you were one. Keeping it separate matters
    practically too — routing a CAPTCHA into the emailed-code path would click
    a send button that is not there, then wait two minutes for mail nobody
    sent, and burn the single attempt allowed per expiry.
    """
    for marker in HUMAN_ONLY_MARKERS:
        if marker in text:
            return marker
    return None




def country_for(base_url: str) -> str:
    """Canada for the .ca site, United States for .com."""
    for host, country in COUNTRY_BY_HOST.items():
        if host in (base_url or ""):
            return country
    return "Canada"


def _dismiss_consent(page: Any) -> None:
    """The cookie modal renders over the login form and eats the clicks.

    It is present on the auth domain too, not just the hiring site — which is
    why dismissing it once before navigating here was not enough.
    """
    try:
        consent = page.locator(CONSENT_BUTTON).first
        if consent.count() and consent.is_visible():
            consent.click(timeout=8000)
            page.wait_for_timeout(800)
    except Exception as exc:  # noqa: BLE001 - absence is the normal case
        log.debug("no consent modal: %s", exc)


def _select_country(page: Any, country: str, *, timeout_ms: int = 20000) -> bool:
    """Pick the country. Required, and the form will not proceed without it."""
    try:
        page.locator(COUNTRY_TOGGLE).first.click(timeout=timeout_ms)
        page.wait_for_timeout(800)
        option = page.locator(f"{COUNTRY_OPTION}:has-text('{country}')").first
        option.click(timeout=timeout_ms)
        page.wait_for_timeout(600)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("could not select the country %r: %s", country, exc)
        return False

def _enter_pin(page: Any, pin: str, *, timeout_ms: int = 20000,
               selector: str | None = None) -> bool:
    """Type the PIN, whether it is one field or six little boxes.

    Split-digit inputs are common for PIN entry and a plain fill() on the
    first box would silently enter one character, which reads as a wrong PIN
    and burns an attempt.
    """
    try:
        boxes = page.locator(PIN_BOXES)
        count = boxes.count()
    except Exception:  # noqa: BLE001
        count = 0

    if count >= len(pin) > 0:
        try:
            for index, digit in enumerate(pin):
                boxes.nth(index).fill(digit)
            return True
        except Exception as exc:  # noqa: BLE001 - fall through to one field
            log.debug("split PIN entry failed: %s", exc)

    try:
        field = page.locator(selector or PIN_INPUT).first
        field.wait_for(state="visible", timeout=timeout_ms)
        field.fill(pin)
        return True
    except Exception as exc:  # noqa: BLE001
        log.debug("single PIN field not found: %s", exc)
        return False

def _solve_email_code(page: Any, *, timeout_ms: int = 20000) -> tuple[str, str] | None:
    """Ask for the emailed code, read it out of the mailbox, and type it in.

    Returns None when no mailbox is configured, leaving the caller to report
    the challenge exactly as it did before. Never called for a CAPTCHA.
    """
    if otp_mail.configured() is None:
        return None

    requested_at = time.time()
    try:
        send = page.locator(SEND_CODE_BUTTON).first
        if send.count():
            send.click(timeout=timeout_ms)
            page.wait_for_timeout(2000)
            log.info("asked Amazon to email a verification code")
    except Exception as exc:  # noqa: BLE001
        return UNKNOWN, f"could not request the verification code: {str(exc)[:120]}"

    # Only a code that arrives AFTER this moment counts — see otp_mail.
    code = otp_mail.fetch_code(requested_at)
    if not code:
        return OTP_REQUIRED, (
            "the code was requested but never arrived within the wait — check "
            "OTP_IMAP_USER / OTP_IMAP_PASSWORD, or enter it yourself"
        )

    if not _enter_code(page, code, timeout_ms=timeout_ms):
        return UNKNOWN, "the code arrived but no field appeared to type it into"

    try:
        verify = page.locator(VERIFY_BUTTON).first
        if not verify.count():
            verify = page.locator(SUBMIT_BUTTON).first
        verify.click(timeout=timeout_ms)
        page.wait_for_timeout(6000)
    except Exception as exc:  # noqa: BLE001
        return UNKNOWN, f"could not submit the verification code: {str(exc)[:120]}"

    text = _page_text(page)
    if _is_captcha(text):
        return CAPTCHA, "a CAPTCHA appeared after the verification code"
    if _needs_a_human(text):
        return OTP_REQUIRED, "Amazon asked for another challenge after the code"
    if any(marker in text for marker in WRONG_CREDENTIAL_MARKERS):
        return BAD_CREDENTIALS, "the verification code was rejected"
    return OK, "signed in after an emailed verification code"


def _enter_code(page: Any, code: str, *, timeout_ms: int = 20000) -> bool:
    """Same shapes as a PIN — one field, or one box per digit."""
    return _enter_pin(page, code, timeout_ms=timeout_ms, selector=CODE_INPUT)


def attempt(page: Any, base_url: str, *, timeout_ms: int = 20000) -> tuple[str, str]:
    """Try to sign in once. Returns (status, detail).

    Never raises: a failed re-login must leave the watcher detecting and
    alerting exactly as it was.
    """
    creds = credentials()
    if creds is None:
        return UNKNOWN, "no credentials in .env (AMAZON_LOGIN_EMAIL / AMAZON_LOGIN_PASSWORD)"
    email, pin = creds

    try:
        page.goto(base_url.rstrip("/") + "/app#/jobSearch", wait_until="domcontentloaded")
        page.wait_for_timeout(3000)

        # Consent modal first: its backdrop swallows clicks.
        try:
            consent = page.locator(CONSENT_BUTTON).first
            if consent.count() and consent.is_visible():
                consent.click(timeout=5000)
                page.wait_for_timeout(500)
        except Exception as exc:  # noqa: BLE001 - usually absent
            log.debug("no consent modal: %s", exc)

        # Go to the login form itself.
        page.goto("https://auth.hiring.amazon.com/#/login", wait_until="domcontentloaded")
        page.wait_for_timeout(4000)

        # Before typing anything: is this already a challenge?
        text = _page_text(page)
        stopper = _is_captcha(text)
        if stopper:
            return CAPTCHA, f"the login page is showing a {stopper!r} — only you can clear it"
        blocker = _needs_a_human(text)
        if blocker:
            return OTP_REQUIRED, f"the login page is asking for {blocker!r}"

        # The consent modal is on this domain too, and its backdrop swallows
        # every click on the form behind it.
        _dismiss_consent(page)

        country = country_for(base_url)
        if not _select_country(page, country, timeout_ms=timeout_ms):
            return UNKNOWN, f"could not select the country {country!r} on the login form"

        field = page.locator(EMAIL_INPUT).first
        field.wait_for(state="visible", timeout=timeout_ms)
        field.fill(email)
        page.locator(CONTINUE_BUTTON).first.click(timeout=timeout_ms)
        page.wait_for_timeout(5000)

        text = _page_text(page)
        stopper = _is_captcha(text)
        if stopper:
            return CAPTCHA, f"a {stopper!r} appeared after the email — only you can clear it"
        blocker = _needs_a_human(text)
        if blocker:
            solved = _solve_email_code(page, timeout_ms=timeout_ms)
            if solved is not None:
                return solved
            return OTP_REQUIRED, f"Amazon asked for {blocker!r} — a human is needed"

        if not _enter_pin(page, pin, timeout_ms=timeout_ms):
            return UNKNOWN, (
                "no PIN field appeared after the email step — the login flow "
                "has changed, or the account uses a different method"
            )
        page.locator(SUBMIT_BUTTON).first.click(timeout=timeout_ms)
        page.wait_for_timeout(6000)

        text = _page_text(page)
        stopper = _is_captcha(text)
        if stopper:
            return CAPTCHA, f"a {stopper!r} appeared after the PIN — only you can clear it"
        blocker = _needs_a_human(text)
        if blocker:
            solved = _solve_email_code(page, timeout_ms=timeout_ms)
            if solved is not None:
                return solved
            return OTP_REQUIRED, f"Amazon asked for {blocker!r} after the PIN"
        if any(marker in text for marker in WRONG_CREDENTIAL_MARKERS):
            # Do not try again. Two wrong passwords in a row is how a lock
            # starts, and a locked account costs every future shift.
            return BAD_CREDENTIALS, "the email or PIN was rejected — check .env"

        return OK, "signed in"
    except Exception as exc:  # noqa: BLE001 - never take the watcher down
        return UNKNOWN, f"re-login attempt failed: {str(exc)[:200]}"

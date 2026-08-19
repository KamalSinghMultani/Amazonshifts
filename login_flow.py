"""Automated re-login, for when the session dies while nobody is watching.

The hiring-portal session lasts about two hours — measured, and consistent
with the competing service re-authenticating on a timer rather than trying to
keep one alive. Detection carries on working when it expires, which is the
trap: the watcher looks healthy and cannot hold a thing.

Needs AMAZON_LOGIN_EMAIL and AMAZON_LOGIN_PIN in .env, and
session.auto_relogin: true. Without either, nothing here runs.

Operational notes worth keeping in mind:

* One attempt per expiry, never a loop. Repeated failed logins are how
  accounts get locked, and a locked account costs every future shift.
* The login flow is email -> country -> 6-digit PIN, then sometimes a
  challenge. An emailed code can be finished automatically when a mailbox is
  configured (see otp_mail); an image challenge returns CAPTCHA and stops.
* Success is verified by re-checking the portal afterwards, never inferred
  from a click that did not raise.
"""

from __future__ import annotations

import logging
import os
import re
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

# A CAPTCHA is not answerable from a mailbox, so it must never be routed into
# the email path: that would click a "send code" button which is not there,
# then sit for two minutes waiting on mail nobody sent, and burn the one
# attempt allowed per expiry.
HUMAN_ONLY_MARKERS = (
    "puzzle",
    "captcha",
    "solve this",
    "select all images",
    "are you a robot",
    # CONFIRMED live 2026-08-18, and the wording matters: the screen says
    # "Let's confirm you are human / Choose all the clocks". The list used to
    # say "verify you are human", which missed it — so four attempts clicked
    # Send, sat behind a CAPTCHA that stopped the mail ever being sent, and
    # reported "the code never arrived" as though the mailbox were at fault.
    "confirm you are human",
    "verify you are human",
    "choose all the",
    "select each image",
)

# The AWS WAF challenge renders in its own container regardless of wording,
# which survives Amazon rephrasing the text.
CAPTCHA_SELECTORS = (
    "[id*='captcha' i]",
    "[class*='captcha' i]",
    "iframe[src*='captcha' i]",
    "iframe[title*='challenge' i]",
    "[data-test-id*='captcha' i]",
)


def captcha_on_screen(page: Any) -> bool:
    """Structural check, for when the wording changes but the block does not."""
    for selector in CAPTCHA_SELECTORS:
        try:
            if page.locator(selector).first.count():
                return True
        except Exception:  # noqa: BLE001 - a bad selector must not be fatal
            continue
    return False

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


# Amazon states where it is sending the code, masked: "Email verification code
# to t*********n@gmail.com". Capturing it turns the single most likely setup
# mistake — reading the wrong mailbox — from a silent timeout into a message
# that names both addresses.
CODE_DESTINATION = re.compile(r"code to ([a-z0-9._%+*-]+@[a-z0-9.-]+)", re.I)


def code_destination(text: str) -> str:
    match = CODE_DESTINATION.search(text or "")
    return match.group(1) if match else ""


def mailbox_matches(destination: str, mailbox: str) -> bool | None:
    """Could a code sent to `destination` land in `mailbox`?

    Amazon masks the middle of the address, so this compares only what is
    visible: the first character, the last character before the @, and the
    domain. Returns None when there is not enough to judge — forwarding also
    makes a mismatch perfectly workable, so this only ever warns.
    """
    if not destination or not mailbox or "@" not in destination:
        return None
    local, _, domain = destination.partition("@")
    box_local, _, box_domain = mailbox.partition("@")
    if not local or not box_local or domain.lower() != box_domain.lower():
        return False
    if local[0].lower() != box_local[0].lower():
        return False
    if local[-1] != "*" and local[-1].lower() != box_local[-1].lower():
        return False
    return True


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
    """An image challenge, as opposed to an emailed code.

    Kept separate because the two need completely different handling: one can
    be finished from the mailbox, the other cannot. Routing a CAPTCHA into the
    emailed-code path clicks a send button that is not there, waits two minutes
    for mail nobody sent, and burns the single attempt allowed per expiry.
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

    destination = code_destination(_page_text(page))
    mailbox = (otp_mail.configured() or ("", "", ""))[1]
    if destination:
        log.info("Amazon will send the code to %s", destination)

    requested_at = time.time()
    try:
        send = page.locator(SEND_CODE_BUTTON).first
        if not send.count():
            # Never wait for mail nobody asked for. Four attempts reported "the
            # code never arrived" when the truth was that it was never
            # requested — the button was not found and the click was skipped,
            # which is a completely different problem to diagnose.
            snippet = " | ".join(_page_text(page).split(chr(10)))[:220]
            return UNKNOWN, (
                f"could not find the button that sends the code "
                f"({SEND_CODE_BUTTON}). The screen says: {snippet}"
            )
        send.click(timeout=timeout_ms)
        page.wait_for_timeout(2500)
        log.info("asked Amazon to email a verification code")

        # Confirm Amazon acted on it. The code-entry screen says "A
        # verification code has been sent to …"; without that, waiting is
        # pointless and the real problem is on this screen.
        after = _page_text(page)
        blocker = _is_captcha(after)
        if blocker or captcha_on_screen(page):
            # No code was sent, so waiting on the mailbox is pointless.
            return CAPTCHA, (
                f"a CAPTCHA appeared when asking for the code "
                f"({blocker or 'challenge widget on screen'}). No code was "
                "sent, so no mailbox setting would have helped."
            )
        if "has been sent" not in after and "enter the verification code" not in after:
            snippet = " | ".join(after.split(chr(10)))[:220]
            return UNKNOWN, (
                "the send button was clicked but Amazon never confirmed sending "
                f"a code. The screen says: {snippet}"
            )
    except Exception as exc:  # noqa: BLE001
        return UNKNOWN, f"could not request the verification code: {str(exc)[:120]}"

    # Only a code that arrives AFTER this moment counts — see otp_mail.
    code = otp_mail.fetch_code(requested_at)
    if not code:
        # Name both addresses. The commonest setup mistake is reading a
        # different mailbox from the one Amazon mails, and a bare timeout gives
        # no hint of that at all.
        where = f" Amazon sent it to {destination}." if destination else ""
        reading = f" We are reading {mailbox}." if mailbox else ""
        hint = ""
        if mailbox_matches(destination, mailbox) is False:
            hint = (
                " Those are different mailboxes — either point OTP_IMAP_USER at "
                "the one Amazon mails, or forward Amazon's code mail from it to "
                "the one being read."
            )
        return OTP_REQUIRED, (
            "the code was requested but never arrived within the wait."
            f"{where}{reading}{hint}"
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

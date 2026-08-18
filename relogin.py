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
from typing import Any

log = logging.getLogger(__name__)

# CONFIRMED live 2026-08-17 against auth.hiring.amazon.com/#/login.
EMAIL_INPUT = "#login, [data-test-id='input-test-id-login']"
CONTINUE_BUTTON = "[data-test-id='button-continue']"
CONSENT_BUTTON = "[data-test-id='consentBtn']"

# The screen after Continue was NOT captured — reaching it requires submitting
# a real email, which would mean an unsolicited login attempt on someone's
# account. So the password field is found by trying what it plausibly is,
# widest-net first, and the attempt reports honestly when it finds nothing.
PASSWORD_INPUT = (
    "input[type='password'], "
    "[data-test-id='input-test-id-password'], "
    "[data-test-id*='password'] input, "
    "#password"
)
SUBMIT_BUTTON = (
    "[data-test-id='button-signIn'], "
    "[data-test-id='button-continue'], "
    "button[type='submit']"
)

# Text that means a human is required. Checked before anything is typed.
OTP_MARKERS = (
    "verification code",
    "one-time password",
    "one time password",
    "we sent a code",
    "enter the code",
    "check your email",
    "puzzle",
    "captcha",
)
WRONG_CREDENTIAL_MARKERS = (
    "incorrect",
    "does not match",
    "not recognised",
    "not recognized",
    "try again",
)

OK = "ok"
OTP_REQUIRED = "otp_required"
BAD_CREDENTIALS = "bad_credentials"
UNKNOWN = "unknown"


def credentials() -> tuple[str, str] | None:
    """Read them from the environment, never from config.yaml.

    config.yaml is committed to git; .env is not, and is already where the
    Telegram token lives.
    """
    email = os.getenv("AMAZON_LOGIN_EMAIL", "").strip()
    password = os.getenv("AMAZON_LOGIN_PASSWORD", "").strip()
    if not email or not password:
        return None
    return email, password


def _page_text(page: Any) -> str:
    try:
        return (page.inner_text("body") or "").lower()
    except Exception as exc:  # noqa: BLE001 - mid-navigation is normal
        log.debug("could not read login page text: %s", exc)
        return ""


def _needs_a_human(text: str) -> str | None:
    for marker in OTP_MARKERS:
        if marker in text:
            return marker
    return None


def attempt(page: Any, base_url: str, *, timeout_ms: int = 20000) -> tuple[str, str]:
    """Try to sign in once. Returns (status, detail).

    Never raises: a failed re-login must leave the watcher detecting and
    alerting exactly as it was.
    """
    creds = credentials()
    if creds is None:
        return UNKNOWN, "no credentials in .env (AMAZON_LOGIN_EMAIL / AMAZON_LOGIN_PASSWORD)"
    email, password = creds

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
        blocker = _needs_a_human(_page_text(page))
        if blocker:
            return OTP_REQUIRED, f"the login page is asking for {blocker!r}"

        field = page.locator(EMAIL_INPUT).first
        field.wait_for(state="visible", timeout=timeout_ms)
        field.fill(email)
        page.locator(CONTINUE_BUTTON).first.click(timeout=timeout_ms)
        page.wait_for_timeout(4000)

        text = _page_text(page)
        blocker = _needs_a_human(text)
        if blocker:
            # The common case on an untrusted device, and the reason this
            # feature can never be relied on completely.
            return OTP_REQUIRED, f"Amazon asked for {blocker!r} — a human is needed"

        secret = page.locator(PASSWORD_INPUT).first
        try:
            secret.wait_for(state="visible", timeout=timeout_ms)
        except Exception:  # noqa: BLE001
            return UNKNOWN, (
                "no password field appeared after the email step — the login "
                "flow has changed, or the account uses a different method"
            )

        secret.fill(password)
        page.locator(SUBMIT_BUTTON).first.click(timeout=timeout_ms)
        page.wait_for_timeout(6000)

        text = _page_text(page)
        blocker = _needs_a_human(text)
        if blocker:
            return OTP_REQUIRED, f"Amazon asked for {blocker!r} after the password"
        if any(marker in text for marker in WRONG_CREDENTIAL_MARKERS):
            # Do not try again. Two wrong passwords in a row is how a lock
            # starts, and a locked account costs every future shift.
            return BAD_CREDENTIALS, "the credentials were rejected — check .env"

        return OK, "signed in"
    except Exception as exc:  # noqa: BLE001 - never take the watcher down
        return UNKNOWN, f"re-login attempt failed: {str(exc)[:200]}"

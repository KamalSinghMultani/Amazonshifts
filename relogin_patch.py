"""Focused runtime fixes for relogin.py.

Kept separate so the large, battle-tested CAPTCHA/login module does not need a
broad rewrite. apply_patch() replaces only the two methods covered by the
regressions found in the PR test run.
"""

from __future__ import annotations

import time


def apply_patch(module) -> None:
    """Install the corrected authentication-state methods on *module*."""

    AuthState = module.AuthState
    CaptchaType = module.CaptchaType
    EMAIL_INPUT = module.EMAIL_INPUT
    CODE_INPUT = module.CODE_INPUT
    COUNTRY_TOGGLE = module.COUNTRY_TOGGLE
    PIN_INPUT_SELECTORS = module.PIN_INPUT_SELECTORS
    SEND_CODE_BUTTON = module.SEND_CODE_BUTTON

    def _is_authenticated(self) -> bool:
        """Require positive account evidence; a hiring.amazon URL is not enough.

        The public job-search URL is reachable while signed out, so URL-only
        detection produced false SESSION_READY results. Negative login evidence
        wins first, then explicit account UI/text is required for a positive.
        """
        url = (self.page.url or "").lower().strip()

        if url in ("", "about:blank", "chrome://newtab/"):
            return False
        if "auth.hiring.amazon" in url:
            return False

        for selector in (EMAIL_INPUT, CODE_INPUT, COUNTRY_TOGGLE, *PIN_INPUT_SELECTORS):
            if self._is_visible(selector):
                return False

        text = self._get_text()
        if any(phrase in text for phrase in (
            "enter your personal pin",
            "verification code",
            "email or mobile number",
            "select your country",
            "where should we send",
            "send verification code",
        )):
            return False

        # Positive evidence only. The URL deliberately does NOT count.
        if any(marker in text for marker in (
            "my account",
            "sign out",
            "welcome back",
        )):
            return True

        for selector in (
            "[data-test-id*='dashboard']",
            "[data-test-id*='user-menu']",
            "[class*='user-menu']",
        ):
            if self._is_visible(selector):
                return True

        return False

    def _request_otp(self) -> bool:
        """After Send, accept either OTP entry or CAPTCHA as the next state."""
        if not self.detector._is_visible(SEND_CODE_BUTTON):
            module.log.warning("Send code button is not visible")
            return False

        self.page.locator(SEND_CODE_BUTTON).first.click()
        self.otp_requested_at = time.time()
        self._log_transition("OTP_REQUESTED")

        deadline = time.time() + 60
        while time.time() < deadline:
            state = self.detector.detect_state()

            if state == AuthState.OTP_ENTRY_REQUIRED:
                self.state = AuthState.OTP_ENTRY_REQUIRED
                self._log_transition("OTP_ENTRY_REQUIRED")
                return True

            if state == AuthState.CAPTCHA_REQUIRED:
                self.state = AuthState.CAPTCHA_REQUIRED
                captype = self.detector.detect_captcha_type()
                if captype == CaptchaType.NONE:
                    captype = CaptchaType.UNKNOWN
                self._log_transition(f"CAPTCHA_DETECTED:{captype.name}")
                return True

            time.sleep(0.5)

        module.log.warning("No OTP entry or CAPTCHA appeared within 60 seconds")
        return False

    module.StateDetector._is_authenticated = _is_authenticated
    module.AuthenticationStateMachine._request_otp = _request_otp

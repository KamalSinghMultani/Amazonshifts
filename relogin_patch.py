"""Focused runtime fixes for relogin.py.

Kept separate so the large, battle-tested CAPTCHA/login module does not need a
broad rewrite. apply_patch() replaces only the methods covered by regressions
and live evidence from the Hiring authentication flow.
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

    def _application_auth_evidence(self) -> bool:
        """Recognize the protected application consent state without token values.

        Live evidence after the WAF challenge cleared showed Amazon returning to
        `/application/ca/#/consent` with the consent UI mounted and auth-token
        *keys* present in localStorage. URL alone is deliberately insufficient:
        we require both the protected application UI and token-key structure.
        No token/storage values are read or logged.
        """
        url = (self.page.url or "").lower().strip()
        if "hiring.amazon." not in url or "/application/" not in url:
            return False

        try:
            evidence = self.page.evaluate(
                """() => {
                    const text = (document.body?.innerText || '').toLowerCase();
                    const buttons = Array.from(document.querySelectorAll('button'));
                    const createVisible = buttons.some(btn => {
                        const s = getComputedStyle(btn);
                        const r = btn.getBoundingClientRect();
                        const visible = s.visibility !== 'hidden' && s.display !== 'none' &&
                                        r.width > 0 && r.height > 0;
                        const label = (btn.innerText || btn.textContent || '').toLowerCase();
                        return visible && label.includes('create application');
                    });
                    const consentMounted =
                        text.includes('by applying, you confirm that') || createVisible;
                    const keys = new Set(Object.keys(localStorage));
                    const tokenStructure = keys.has('accessToken') || keys.has('idToken');
                    return {consentMounted, tokenStructure};
                }"""
            )
            return bool(
                isinstance(evidence, dict)
                and evidence.get("consentMounted")
                and evidence.get("tokenStructure")
            )
        except Exception:
            return False

    def _is_authenticated(self) -> bool:
        """Require positive account/application evidence; URL alone is not enough.

        The public job-search URL is reachable while signed out, so URL-only
        detection produced false SESSION_READY results. Negative login evidence
        wins first, then explicit account UI/text or protected-application
        evidence is required for a positive.
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

        # Live Canadian auth flow can land directly on the protected consent
        # page without rendering the generic account-menu markers above.
        if _application_auth_evidence(self):
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

    module.StateDetector._application_auth_evidence = _application_auth_evidence
    module.StateDetector._is_authenticated = _is_authenticated
    module.AuthenticationStateMachine._request_otp = _request_otp

"""Focused runtime fixes for relogin.py.

Kept separate so the large, battle-tested CAPTCHA/login module does not need a
broad rewrite. apply_patch() replaces only the methods covered by regressions
and live evidence from the Hiring authentication flow.
"""

from __future__ import annotations

import time
from urllib.parse import quote, urlencode, urlparse


def auth_entry_url(base_url: str) -> str:
    """Build the non-destructive Hiring login URL with Amazon's own callback.

    A real Apply redirect does not enter the auth SPA at bare ``#/login``. It
    carries a country/locale plus ``redirectUrl`` back through ``app#/auth-return``
    and a ``destinationUrl`` on the country-specific application site. That
    callback is what lets the Hiring frontend finish its post-auth session
    handoff. Using bare login can leave an authenticated-looking consent shell
    while the protected candidate API still returns 401.

    The destination below is only the generic consent shell. It has no jobId or
    scheduleId, so this login bootstrap cannot create an application or reserve
    a shift.
    """
    base = (base_url or "").rstrip("/")
    host = (urlparse(base).hostname or "").lower()
    if host.endswith("amazon.ca"):
        country_code, locale, application_country = "CA", "en-CA", "ca"
    else:
        country_code, locale, application_country = "US", "en-US", "us"

    redirect_url = base + "/app#/auth-return"
    destination_url = base + f"/application/{application_country}/#/consent"
    query = urlencode(
        {
            "countryCode": country_code,
            "locale": locale,
            "onDemandSync": "true",
            "referrer": "CS",
            "redirectUrl": redirect_url,
            "destinationUrl": destination_url,
        },
        quote_via=quote,
        safe="",
    )
    return "https://auth.hiring.amazon.com/#/login?" + query


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

        Live Canadian evidence after the WAF challenge cleared showed Amazon
        returning to `/application/ca/#/consent` with:

        * the application layout mounted;
        * the consent-page title/body state;
        * no login controls; and
        * both `accessToken` and `idToken` key names in localStorage.

        URL alone is deliberately insufficient. No token/storage values are read
        or logged; only structural booleans and key *names* are inspected.
        """
        url = (self.page.url or "").lower().strip()
        if "hiring.amazon." not in url or "/application/" not in url:
            return False

        try:
            evidence = self.page.evaluate(
                """() => {
                    const visible = (el) => {
                        if (!el) return false;
                        const s = getComputedStyle(el);
                        const r = el.getBoundingClientRect();
                        return s.visibility !== 'hidden' && s.display !== 'none' &&
                               r.width > 0 && r.height > 0;
                    };
                    const text = (document.body?.innerText || '').toLowerCase();
                    const title = (document.title || '').toLowerCase();
                    const buttons = Array.from(document.querySelectorAll('button'));
                    const createVisible = buttons.some(btn => {
                        const label = (btn.innerText || btn.textContent || '').toLowerCase();
                        return visible(btn) && label.includes('create application');
                    });
                    const loginSelectors = [
                        "[data-test-id='input-test-id-login']",
                        "[data-test-id='input-test-id-confirmOtp'] input",
                        "[data-test-id='input-test-id-pin']",
                        "#country-toggle-button"
                    ];
                    const keys = new Set(Object.keys(localStorage));
                    return {
                        routeConsent:
                            location.pathname.toLowerCase().includes('/application/') &&
                            location.hash.toLowerCase().includes('/consent'),
                        layoutVisible: visible(document.querySelector("[data-test-id='layout']")),
                        titleConsent: title.includes('by applying, you confirm that'),
                        bodyConsent: text.includes('by applying, you confirm that'),
                        createVisible,
                        loginVisible: loginSelectors.some(sel => {
                            try { return visible(document.querySelector(sel)); }
                            catch (_) { return false; }
                        }),
                        tokenStructure: keys.has('accessToken') && keys.has('idToken')
                    };
                }"""
            )
            if not isinstance(evidence, dict):
                return False

            protected_ui = bool(
                evidence.get("routeConsent")
                and evidence.get("layoutVisible")
                and (
                    evidence.get("titleConsent")
                    or evidence.get("bodyConsent")
                    or evidence.get("createVisible")
                )
            )
            return bool(
                protected_ui
                and evidence.get("tokenStructure")
                and not evidence.get("loginVisible")
            )
        except Exception:
            return False

    def _is_authenticated(self) -> bool:
        """Require positive account/application evidence; URL alone is not enough.

        The public job-search URL is reachable while signed out, so URL-only
        detection produced false SESSION_READY results. Negative login controls
        win first. A protected application page is then allowed to prove auth
        before generic text checks, because normal Hiring headers can contain
        phrases such as "Select your country" that also appear in the login UI.

        This remains only a state-machine transition hint. session_refresh.py
        still requires the protected application backend proof before a recovery
        is ever reported successful or imported into the live watcher.
        """
        url = (self.page.url or "").lower().strip()

        if url in ("", "about:blank", "chrome://newtab/"):
            return False
        if "auth.hiring.amazon" in url:
            return False

        # Real visible login controls are authoritative negative evidence.
        for selector in (EMAIL_INPUT, CODE_INPUT, COUNTRY_TOGGLE, *PIN_INPUT_SELECTORS):
            if self._is_visible(selector):
                return False

        # Live Canadian auth can land directly on the protected consent page.
        # Check this BEFORE generic body-text rejection: the ordinary locale
        # header can itself say "Select your country" on an authenticated page.
        if _application_auth_evidence(self):
            return True

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

    def _run(self, base_url: str):
        """Authenticate through the same callback path the real apply flow uses."""
        self._log_transition("AUTH_START")
        self.country = self._country_for(base_url)
        module.log.info("logging in as %s (from %s)", self.country, base_url)

        # Keep the original country-context warmup. auth.hiring.amazon.com is
        # shared by CA/US, so this prevents a cold login from choosing the wrong
        # country before the form has even mounted.
        try:
            self.page.goto(
                base_url.rstrip("/") + "/app#/jobSearch",
                wait_until="domcontentloaded",
            )
            self.page.wait_for_timeout(2000)
        except Exception as exc:
            module.log.debug("could not set the country context: %s", exc)

        # Critical change: do not use bare #/login. Carry Amazon's normal
        # auth-return + destination handoff so a successful login can mint the
        # country-specific application session rather than only painting an
        # authenticated-looking SPA shell.
        self.page.goto(auth_entry_url(base_url), wait_until="domcontentloaded")
        self._wait_for_state()
        self._dismiss_consent()

        max_iterations = 10
        for _ in range(max_iterations):
            self.state = self.detector.detect_state()

            if self.state == AuthState.AUTHENTICATED:
                self._log_transition("AUTH_VERIFIED")
                return self.state

            if self.state in (AuthState.BAD_CREDENTIALS, AuthState.SESSION_ERROR):
                return self.state

            if not self._transition_to_next():
                return AuthState.SESSION_ERROR

        return AuthState.SESSION_ERROR

    module.StateDetector._application_auth_evidence = _application_auth_evidence
    module.StateDetector._is_authenticated = _is_authenticated
    module.AuthenticationStateMachine._request_otp = _request_otp
    module.AuthenticationStateMachine.run = _run

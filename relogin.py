"""
Amazon Hiring re-login automation - Refactored with explicit state machine.
"""

from __future__ import annotations

import logging
from enum import Enum, auto
from typing import Any, Optional, Tuple, Dict
import os
import re
import time

import otp_mail

log = logging.getLogger(__name__)


# ============================================================================
# COMPATIBILITY EXPORTS - required by watcher.py
# ============================================================================

# Status constants (must match what watcher.py expects)
OK = "ok"
OTP_REQUIRED = "otp_required"
CAPTCHA = "captcha"
BAD_CREDENTIALS = "bad_credentials"
UNKNOWN = "unknown"


def credentials() -> tuple[str, str] | None:
    """Email and 6-digit PIN, from the environment only.

    Required by watcher.py which calls this before attempting re-login.
    """
    email = os.getenv("AMAZON_LOGIN_EMAIL", "").strip()
    pin = (os.getenv("AMAZON_LOGIN_PIN") or os.getenv("AMAZON_LOGIN_PASSWORD") or "").strip()
    if not email or not pin:
        return None
    if not (pin.isdigit() and len(pin) == 6):
        log.warning(
            "AMAZON_LOGIN_PIN is %d character(s) and %s all digits; Amazon "
            "Hiring uses a 6-digit PIN",
            len(pin), "not" if not pin.isdigit() else "is",
        )
    return email, pin


# ============================================================================
# AUTHENTICATION STATES
# ============================================================================

class AuthState(Enum):
    """Explicit authentication states."""
    LOGIN_PAGE = auto()
    EMAIL_REQUIRED = auto()
    PIN_REQUIRED = auto()
    CAPTCHA_REQUIRED = auto()
    OTP_SEND_REQUIRED = auto()
    OTP_WAITING = auto()
    OTP_ENTRY_REQUIRED = auto()
    AUTHENTICATED = auto()
    BAD_CREDENTIALS = auto()
    OTP_TIMEOUT = auto()
    CAPTCHA_FAILED = auto()
    SESSION_ERROR = auto()
    UNKNOWN_PAGE = auto()


class CaptchaType(Enum):
    """Different CAPTCHA challenge types."""
    NONE = auto()
    IMAGE_GRID = auto()
    TOKEN = auto()
    TEXT = auto()
    UNKNOWN = auto()


# ============================================================================
# SELECTORS - Prefer exact test IDs, then containers, then fallbacks
# ============================================================================

# Exact Amazon test IDs (highest priority)
EMAIL_INPUT = "[data-test-id='input-test-id-login']"
CONTINUE_BUTTON = "[data-test-id='button-continue']"
CONSENT_BUTTON = "[data-test-id='consentBtn']"
SEND_CODE_BUTTON = "[data-test-id='button-submit']"
# The test-id belongs to the WRAPPER; the field is a bare <input> inside it,
# with maxlength=6 and no test-id of its own (confirmed on the live code
# screen). Filling the wrapper raises "Element is not an <input>", which is
# what stopped the login after the code had already been read from the inbox.
CODE_INPUT = "[data-test-id='input-test-id-confirmOtp'] input"
VERIFY_BUTTON = "[data-test-id='button-test-id-verifyAccount']"
COUNTRY_TOGGLE = "#country-toggle-button"

# PIN selectors (tuple of selectors, not one string)
PIN_INPUT_SELECTORS = (
    "[data-test-id='input-test-id-pin']",
    "[data-test-id*='pin'] input",
    "input[inputmode='numeric'][maxlength='6']",
)

# Submit buttons (tuple, not string)
SUBMIT_BUTTON_SELECTORS = (
    "[data-test-id='button-signIn']",
    "[data-test-id='button-login']",
    "[data-test-id='button-continue']",
)

# Image grid CAPTCHA selectors
GRID_IMAGE_SELECTORS = (
    "[data-test-id*='image-grid'] img",
    "[class*='image-grid'] img",
    "[class*='aws-challenge'] img",
    "[class*='challenge'] img",
)

# Amazon's human-verification challenge is rendered in a cross-origin AWS WAF
# iframe. Main-page body text/locators cannot see its contents.
AWS_WAF_FRAME_URL_MARKERS = (
    "edge.sdk.awswaf.com",
    "awswaf.com",
)


GRID_CONFIRM_BUTTONS = (
    "button:has-text('Confirm')",
    "button:has-text('Submit')",
    "[data-test-id*='confirm']",
)

# Country selector (needed for login flow)
COUNTRY_OPTION = "li[role='option']"
COUNTRY_BY_HOST = {
    "hiring.amazon.ca": "Canada",
    "hiring.amazon.com": "United States",
}


# ============================================================================
# STATE DETECTION
# ============================================================================

class StateDetector:
    """Centralized state detection - single source of truth."""
    
    def __init__(self, page: Any):
        self.page = page
    
    def _is_visible(self, selector: str) -> bool:
        """Check if element is actually visible, not just in DOM."""
        try:
            elem = self.page.locator(selector).first
            return elem.count() > 0 and elem.is_visible()
        except Exception:
            return False
    
    def captcha_frame(self):
        """Return the live AWS WAF challenge frame when present, otherwise None.

        This is detection/diagnostic plumbing only. It does not solve the
        challenge.
        """
        try:
            for frame in self.page.frames:
                url = (frame.url or "").lower()
                if any(marker in url for marker in AWS_WAF_FRAME_URL_MARKERS):
                    return frame
        except Exception as exc:
            log.debug("could not enumerate frames: %s", exc)
        return None

    def _frame_text(self, frame: Any) -> str:
        """Best-effort visible text from a frame."""
        try:
            return (frame.locator("body").inner_text() or "").lower()
        except Exception:
            return ""

    def _get_text(self) -> str:
        """Get visible text from the main document plus the AWS WAF challenge.

        The CAPTCHA wording lives in a separate iframe, so main-document
        inner_text alone can never see phrases such as "Choose all the ...".
        """
        parts = []
        try:
            main_text = (self.page.inner_text("body") or "").lower()
            if main_text:
                parts.append(main_text)
        except Exception:
            pass

        frame = self.captcha_frame()
        if frame is not None:
            frame_text = self._frame_text(frame)
            if frame_text:
                parts.append(frame_text)

        return "\n".join(parts)
    
    def detect_state(self) -> AuthState:
        """Detect current authentication state from page."""
        text = self._get_text()
        
        # Check for authenticated state first (most specific)
        if self._is_authenticated():
            return AuthState.AUTHENTICATED
        
        # Check for explicit error states
        if self._is_bad_credentials(text):
            return AuthState.BAD_CREDENTIALS
        
        # Check for CAPTCHA
        if self.detect_captcha_type() != CaptchaType.NONE:
            return AuthState.CAPTCHA_REQUIRED
        
        # Check for OTP entry
        if self._is_otp_entry(text):
            return AuthState.OTP_ENTRY_REQUIRED
        
        # Check for OTP send
        if self._is_otp_send(text):
            return AuthState.OTP_SEND_REQUIRED
        
        # Check for PIN
        if self._is_pin_required():
            return AuthState.PIN_REQUIRED
        
        # Check for email
        if self._is_email_required():
            return AuthState.EMAIL_REQUIRED
        
        return AuthState.UNKNOWN_PAGE
    
    def detect_captcha_type(self) -> CaptchaType:
        """Detect CAPTCHA type based on frame presence, structure and text."""
        text = self._get_text()
        frame = self.captcha_frame()

        # Amazon's human-verification challenge is hosted in an AWS WAF frame.
        # Classify the observed "choose/select all ..." wording from that frame
        # directly, rather than looking for tiles on the main document.
        if frame is not None:
            frame_text = self._frame_text(frame)
            challenge_text = frame_text or text

            grid_markers = (
                "choose all the",
                "select all",
                "choose all",
                "confirm you are human",
                "verify you are human",
            )

            if any(marker in challenge_text for marker in grid_markers):
                # If images are visible, this is positively an image-grid.
                # Even when the tile markup changes, the known AWS WAF frame
                # plus the observed grid wording is sufficient to route it as
                # IMAGE_GRID instead of UNKNOWN.
                if self._is_image_grid(challenge_text, frame=frame):
                    return CaptchaType.IMAGE_GRID

                log.info(
                    "AWS WAF challenge uses image-grid wording but tile markup "
                    "did not match known selectors; routing as IMAGE_GRID"
                )
                return CaptchaType.IMAGE_GRID

            # Known challenge frame, but its exact challenge type is unclear.
            log.info("AWS WAF challenge frame detected but type is unknown: %s", frame.url)
            return CaptchaType.UNKNOWN

        # Main-document fallbacks for other challenge implementations.
        if self._is_image_grid(text):
            return CaptchaType.IMAGE_GRID

        if self._is_token_captcha():
            return CaptchaType.TOKEN

        if self._is_text_captcha(text):
            return CaptchaType.TEXT

        if any(m in text for m in (
            "confirm you are human",
            "verify you are human",
            "choose all the",
            "select all images",
            "captcha",
            "puzzle",
        )):
            log.warning("challenge wording detected but type could not be classified")
            return CaptchaType.UNKNOWN

        return CaptchaType.NONE
    
    def _is_authenticated(self) -> bool:
        """Is this session signed in? Observed, never navigated.

        Two failures shaped this, in opposite directions.

        It used to return True for any URL containing "/app" — which it does
        throughout the login flow — so four re-logins declared success 22 to
        162ms after pressing Continue, before any page could load.

        The fix for that was worse: asking the portal meant NAVIGATING, and
        detect_state() calls this constantly. It sailed away from the login
        form mid-flow and reported SESSION_READY in 4.6 seconds without ever
        submitting an email. A state detector must not move the page it is
        detecting.

        So this only reads what is in front of it. The authoritative check
        stays where it belongs: the caller verifies afterwards, once, when
        navigating is safe.
        """
        current_url = self.page.url or ""

        # On the login domain, nothing else matters.
        if "auth.hiring.amazon" in current_url:
            return False

        # A login form on screen means the flow is still in progress.
        for selector in (EMAIL_INPUT, CODE_INPUT, COUNTRY_TOGGLE, *PIN_INPUT_SELECTORS):
            if self._is_visible(selector):
                return False

        text = self._get_text()
        if any(phrase in text for phrase in (
            "enter your personal pin",
            "verification code",
            "email or mobile number",
            "select your country",
        )):
            return False

        # Positive evidence, when the viewport is wide enough to render it.
        # Headless collapses the nav behind a hamburger, so absence proves
        # nothing — which is exactly why the caller verifies separately.
        if any(marker in text for marker in ("my account", "sign out", "welcome back")):
            return True

        for selector in ("[data-test-id*='dashboard']", "[data-test-id*='user-menu']",
                         "[class*='user-menu']"):
            if self._is_visible(selector):
                return True

        # Off the auth domain, no login form, no proof either way. Say no: a
        # false yes stops the login flow dead, a false no merely repeats it.
        return False

    def _is_bad_credentials(self, text: str) -> bool:
        """Check for explicit credential rejection only."""
        # Only explicit wrong-credential messages
        bad_markers = (
            "incorrect password",
            "incorrect email",
            "does not match",
            "not recognised",
            "not recognized",
            "invalid credentials",
        )
        
        # Remove "try again" - could be temporary error
        return any(marker in text for marker in bad_markers)
    
    def _is_otp_entry(self, text: str) -> bool:
        """Check if OTP entry screen is visible."""
        otp_entry_markers = (
            "enter the code",
            "enter the verification code",
            "enter verification code",
            "verification code has been sent",
        )
        
        if any(marker in text for marker in otp_entry_markers):
            return self._is_visible(CODE_INPUT) or "verification code" in text
        
        return False
    
    def _is_otp_send(self, text: str) -> bool:
        """Check if OTP send screen is visible."""
        otp_send_markers = (
            "where should we send",
            "verification code",
            "send code",
        )
        
        if any(marker in text for marker in otp_send_markers):
            return self._is_visible(SEND_CODE_BUTTON)
        
        return False
    
    def _is_pin_required(self) -> bool:
        """Check if PIN entry is required."""
        for selector in PIN_INPUT_SELECTORS:
            if self._is_visible(selector):
                return True
        return False
    
    def _is_email_required(self) -> bool:
        """Check if email entry is required."""
        return self._is_visible(EMAIL_INPUT)
    
    def _is_image_grid(self, text: str, frame: Any = None) -> bool:
        """Detect an image-grid CAPTCHA in the supplied document/frame."""
        grid_markers = (
            "choose all the",
            "select all",
            "choose all",
            "confirm you are human",
            "verify you are human",
        )

        if frame is not None:
            frame_text = self._frame_text(frame)
            if frame_text:
                text = frame_text

        if not any(marker in text for marker in grid_markers):
            return False

        root = frame if frame is not None else self.page

        # Use the largest visible count from any selector instead of adding
        # overlapping selector matches. This avoids counting one tile several
        # times when it matches multiple CSS selectors.
        selectors = list(GRID_IMAGE_SELECTORS)
        if frame is not None:
            # Once already scoped to the known AWS WAF challenge document,
            # plain images are a useful fallback if Amazon changes class names.
            selectors.append("img")

        max_visible = 0
        for selector in selectors:
            try:
                elements = root.locator(selector)
                visible = 0
                for i in range(elements.count()):
                    if elements.nth(i).is_visible():
                        visible += 1
                max_visible = max(max_visible, visible)
            except Exception:
                continue

        # The observed challenge is a multi-tile grid. Requiring several
        # images avoids mistaking a logo or decorative image for the grid.
        return max_visible >= 4
    
    def _is_token_captcha(self) -> bool:
        """Detect reCAPTCHA/hCaptcha."""
        token_selectors = (
            "iframe[src*='recaptcha']",
            "iframe[src*='hcaptcha']",
            "[data-sitekey]",
        )
        
        for selector in token_selectors:
            if self._is_visible(selector):
                return True
        
        return False
    
    def _is_text_captcha(self, text: str) -> bool:
        """Detect text-based CAPTCHA."""
        return "captcha" in text and not self._is_image_grid(text)


# ============================================================================
# CAPTCHA SOLVER INTERFACE
# ============================================================================

class CaptchaSolver:
    """Clean solver interface - use mock for testing."""
    
    def solve(self, page: Any, captcha_type: CaptchaType, frame: Any = None) -> bool:
        """Handle CAPTCHA challenge. The frame is supplied when one was detected."""
        raise NotImplementedError


class MockCaptchaSolver(CaptchaSolver):
    """Mock solver for testing - doesn't actually solve CAPTCHAs."""
    
    def __init__(self, should_succeed: bool = True):
        self.should_succeed = should_succeed
    
    def solve(self, page: Any, captcha_type: CaptchaType, frame: Any = None) -> bool:
        log.info("Mock solving %s (challenge_frame=%s)", captcha_type, frame is not None)
        if self.should_succeed:
            # Simulate clicking confirm
            time.sleep(0.5)
            return True
        return False


class TwoCaptchaSolver(CaptchaSolver):
    """Real 2Captcha solver implementation."""
    
    def __init__(self):
        try:
            from twocaptcha import TwoCaptcha
            api_key = os.getenv("TWOCAPTCHA_API_KEY", "")
            self.solver = TwoCaptcha(api_key) if api_key else None
        except ImportError:
            log.warning("2captcha-python not installed - using no solver")
            self.solver = None
    
    def solve(self, page: Any, captcha_type: CaptchaType, frame: Any = None) -> bool:
        if not self.solver:
            log.error("2Captcha API key not configured or library not installed")
            return False
        
        if captcha_type == CaptchaType.IMAGE_GRID:
            return self._solve_image_grid(page, frame)
        elif captcha_type == CaptchaType.TOKEN:
            return self._solve_token(page, frame)
        elif captcha_type == CaptchaType.TEXT:
            return self._solve_text(page, frame)
        
        return False
    
    def _solve_image_grid(self, page: Any, frame: Any = None) -> bool:
        """Image-grid handler placeholder."""
        # TODO: Implement actual 2Captcha image grid solving
        # This would:
        # 1. Screenshot the grid
        # 2. Send to 2Captcha with coordinates method
        # 3. Parse returned tile numbers
        # 4. Click corresponding elements
        log.info("Solving image grid CAPTCHA with 2Captcha")
        return False
    
    def _solve_token(self, page: Any, frame: Any = None) -> bool:
        """Token-challenge handler placeholder."""
        log.info("Solving token CAPTCHA with 2Captcha")
        return False
    
    def _solve_text(self, page: Any, frame: Any = None) -> bool:
        """Text-challenge handler placeholder."""
        log.info("Solving text CAPTCHA with 2Captcha")
        return False


# ============================================================================
# AUTHENTICATION STATE MACHINE
# ============================================================================

class AuthenticationStateMachine:
    """Explicit state machine for authentication flow."""
    
    def __init__(self, page: Any, solver: CaptchaSolver):
        self.page = page
        self.solver = solver
        self.detector = StateDetector(page)
        self.state = AuthState.LOGIN_PAGE
        self.otp_requested_at = None
        self.country = "Canada"
    
    def run(self, base_url: str) -> AuthState:
        """Run authentication until reaching a terminal state."""
        self._log_transition("AUTH_START")

        # Decide the country ONCE, from the site we are watching. It used to be
        # read off self.page.url at the moment the form was filled — by which
        # point the page is always auth.hiring.amazon.COM, so the lookup
        # returned "United States" every single time and every re-login signed
        # in to the American site. The Canadian session stayed dead for 26
        # hours while the login itself looked perfectly healthy.
        self.country = self._country_for(base_url)
        log.info("logging in as %s (from %s)", self.country, base_url)

        # Load the COUNTRY site first. auth.hiring.amazon.com serves both
        # countries and, arrived at cold, returns you to the US site — the
        # post-login page carried Amazon's own banner saying so: "Seems like
        # you're visiting the US website from Canada." A session established
        # over there is no use to a watcher polling hiring.amazon.ca.
        try:
            self.page.goto(base_url.rstrip("/") + "/app#/jobSearch",
                           wait_until="domcontentloaded")
            self.page.wait_for_timeout(2000)
        except Exception as exc:  # noqa: BLE001 - the login can still proceed
            log.debug("could not set the country context: %s", exc)

        self.page.goto("https://auth.hiring.amazon.com/#/login")
        self._wait_for_state()
        
        # Check for consent modal
        self._dismiss_consent()
        
        max_iterations = 10
        for _ in range(max_iterations):
            self.state = self.detector.detect_state()
            
            if self.state == AuthState.AUTHENTICATED:
                self._log_transition("AUTH_VERIFIED")
                return self.state
            
            if self.state in (AuthState.BAD_CREDENTIALS, AuthState.SESSION_ERROR):
                return self.state
            
            # Transition to next state
            if not self._transition_to_next():
                return AuthState.SESSION_ERROR
        
        return AuthState.SESSION_ERROR
    
    def _transition_to_next(self) -> bool:
        """Execute one state transition."""
        if self.state == AuthState.EMAIL_REQUIRED:
            return self._submit_email()
        
        elif self.state == AuthState.PIN_REQUIRED:
            return self._submit_pin()
        
        elif self.state == AuthState.CAPTCHA_REQUIRED:
            return self._solve_captcha()
        
        elif self.state == AuthState.OTP_SEND_REQUIRED:
            return self._request_otp()
        
        elif self.state == AuthState.OTP_ENTRY_REQUIRED:
            return self._submit_otp()
        
        return False
    
    def _dismiss_consent(self) -> None:
        """Dismiss cookie consent modal if present."""
        try:
            consent = self.page.locator(CONSENT_BUTTON).first
            if consent.count() and consent.is_visible():
                consent.click(timeout=5000)
                self.page.wait_for_timeout(500)
        except Exception:
            pass
    
    def _select_country(self, country: str) -> bool:
        """Select country on the login form."""
        try:
            self.page.locator(COUNTRY_TOGGLE).first.click(timeout=10000)
            self.page.wait_for_timeout(500)
            option = self.page.locator(f"{COUNTRY_OPTION}:has-text('{country}')").first
            option.click(timeout=10000)
            self.page.wait_for_timeout(500)
            return True
        except Exception as exc:
            log.warning(f"Could not select country {country}: {exc}")
            return False
    
    def _country_for(self, base_url: str) -> str:
        """Determine country from base URL."""
        for host, country in COUNTRY_BY_HOST.items():
            if host in (base_url or ""):
                return country
        return "Canada"
    
    def _submit_email(self) -> bool:
        """Submit email and wait for next state."""
        email = os.getenv("AMAZON_LOGIN_EMAIL", "")
        if not email:
            log.error("AMAZON_LOGIN_EMAIL not configured")
            return False
        
        try:
            # The country decided at the start of the run, NOT the URL of the
            # page we happen to be on — that is always the .com auth domain.
            country = getattr(self, "country", None) or self._country_for(self.page.url)
            if not self._select_country(country):
                log.warning("Could not select country, continuing anyway")
            
            # Fill email
            email_input = self.page.locator(EMAIL_INPUT).first
            email_input.wait_for(state="visible", timeout=10000)
            email_input.fill(email)
            
            # Click continue
            self.page.locator(CONTINUE_BUTTON).first.click(timeout=10000)
            
            self._log_transition("EMAIL_SUBMITTED")
            return self._wait_for_state([AuthState.PIN_REQUIRED, AuthState.CAPTCHA_REQUIRED])
        except Exception as exc:
            log.error(f"Email submission failed: {exc}")
            return False
    
    def _submit_pin(self) -> bool:
        """Submit PIN and wait for next state."""
        pin = os.getenv("AMAZON_LOGIN_PIN", "")
        if not pin:
            log.error("AMAZON_LOGIN_PIN not configured")
            return False
        
        try:
            # Find PIN input
            pin_input = None
            for selector in PIN_INPUT_SELECTORS:
                if self.detector._is_visible(selector):
                    pin_input = self.page.locator(selector).first
                    break
            
            if not pin_input:
                log.error("PIN input not found")
                return False
            
            pin_input.fill(pin)
            
            # Find and click submit button
            for selector in SUBMIT_BUTTON_SELECTORS:
                if self.detector._is_visible(selector):
                    self.page.locator(selector).first.click()
                    break
            
            self._log_transition("PIN_SUBMITTED")
            return self._wait_for_state([
                AuthState.CAPTCHA_REQUIRED,
                AuthState.OTP_SEND_REQUIRED,
                AuthState.AUTHENTICATED
            ])
        except Exception as exc:
            log.error(f"PIN submission failed: {exc}")
            return False
    
    def _solve_captcha(self) -> bool:
        """Route CAPTCHA handling and verify the page actually leaves the challenge."""
        captcha_type = self.detector.detect_captcha_type()
        self._log_transition(f"CAPTCHA_DETECTED:{captcha_type.name}")

        challenge_frame = self.detector.captcha_frame()
        if challenge_frame is not None:
            log.info("CAPTCHA challenge frame: %s", challenge_frame.url)

        success = self.solver.solve(
            self.page,
            captcha_type,
            frame=challenge_frame,
        )

        if not success:
            self._log_transition("CAPTCHA_FAILED")
            return False

        # A handler reporting success is not sufficient. Require a real next
        # authentication state and verify that the challenge is no longer seen.
        moved_forward = self._wait_for_state([
            AuthState.OTP_SEND_REQUIRED,
            AuthState.OTP_ENTRY_REQUIRED,
            AuthState.AUTHENTICATED,
        ])

        if not moved_forward:
            self._log_transition("CAPTCHA_FAILED:NO_STATE_TRANSITION")
            return False

        if self.detector.detect_captcha_type() != CaptchaType.NONE:
            self._log_transition("CAPTCHA_FAILED:STILL_PRESENT")
            return False

        self._log_transition("CAPTCHA_COMPLETED")
        return True

    def _request_otp(self) -> bool:
        """Request OTP and record timestamp."""
        if self.detector._is_visible(SEND_CODE_BUTTON):
            self.page.locator(SEND_CODE_BUTTON).first.click()
            self.otp_requested_at = time.time()
            self._log_transition("OTP_REQUESTED")
            # Wait for EITHER outcome. Pressing Send does not always produce
            # the code screen: Amazon frequently answers with a challenge
            # instead ("Let's confirm you are human / Choose all the hats").
            # Waiting only for OTP_ENTRY_REQUIRED meant twenty seconds spent
            # watching for a screen that cannot appear until the challenge is
            # cleared, then a timeout — while detect_state() was never asked,
            # so the solver was never invoked and the run died at
            # OTP_SEND_REQUIRED with the CAPTCHA still on screen.
            #
            # Returning on CAPTCHA_REQUIRED hands control back to the run loop,
            # which routes it to _solve_captcha and comes back here afterwards.
            return self._wait_for_state([
                AuthState.OTP_ENTRY_REQUIRED,
                AuthState.CAPTCHA_REQUIRED,
            ])
        
        return False
    
    def _submit_otp(self) -> bool:
        """Submit OTP and verify authentication."""
        if not self.otp_requested_at:
            return False
        
        code = otp_mail.fetch_code(self.otp_requested_at)
        if not code:
            self._log_transition("OTP_TIMEOUT")
            return False
        
        self._log_transition("OTP_RECEIVED")
        
        try:
            # Enter code. login_flow.enter_code knows both layouts this site
            # uses — one input inside the wrapper, and six single-character
            # boxes — and falls through maxlength/inputmode/tel variants. The
            # field carries no test-id of its own, so a single hard-coded
            # selector is the thing most likely to rot here.
            import login_flow

            entered = login_flow.enter_code(self.page, code)
            if not entered:
                code_input = self.page.locator(CODE_INPUT).first
                if code_input.count() and code_input.is_visible():
                    code_input.fill(code)
                    entered = True

            if entered:
                if login_flow.submit_code(self.page):
                    self._log_transition("OTP_SUBMITTED")
                    return self._wait_for_state([AuthState.AUTHENTICATED])
                log.error("code entered but Verify could not be pressed")
            else:
                log.error("code %s could not be typed into any field on screen", "*" * len(code))
        except Exception as exc:
            log.error(f"OTP submission failed: {exc}")
        
        return False
    
    def _wait_for_state(self, expected_states: list = None, timeout_ms: int = 20000) -> bool:
        """Wait until the page reaches a recognizable or explicitly expected state."""
        start_time = time.time()

        while time.time() - start_time < timeout_ms / 1000:
            self.state = self.detector.detect_state()

            if expected_states is None:
                if self.state != AuthState.UNKNOWN_PAGE:
                    return True
            elif self.state in expected_states:
                return True

            time.sleep(0.5)

        log.warning("Timeout waiting for states: %s", expected_states)
        return False
    
    def _log_transition(self, transition: str):
        """Log state transition without secrets."""
        log.info(f"AUTH_STATE: {transition}")


# ============================================================================
# SESSION MANAGER (Separate from auth)
# ============================================================================

class SessionManager:
    """Manages session lifecycle separately from authentication."""
    
    def __init__(self, page: Any, auth_machine: AuthenticationStateMachine):
        self.page = page
        self.auth_machine = auth_machine
        self.detector = StateDetector(page)
    
    def ensure_session(self, base_url: str) -> bool:
        """Ensure valid session exists, creating if necessary."""
        
        # Try existing session first
        if self._is_session_valid():
            log.info("SESSION_READY")
            return True
        
        # Run authentication
        result = self.auth_machine.run(base_url)
        
        if result == AuthState.AUTHENTICATED:
            self._persist_session()
            log.info("SESSION_READY")
            return True
        
        return False
    
    def _is_session_valid(self) -> bool:
        """Check if current session is authenticated."""
        return self.detector.detect_state() == AuthState.AUTHENTICATED
    
    def _persist_session(self):
        """Persist session state."""
        # Implementation would save cookies/tokens
        pass


# ============================================================================
# MAIN ENTRY POINT
# ============================================================================

def create_auth_system(page: Any, use_mock_solver: bool = True) -> SessionManager:
    """Factory function to create authentication system."""
    
    solver = MockCaptchaSolver() if use_mock_solver else TwoCaptchaSolver()
    
    auth_machine = AuthenticationStateMachine(page, solver)
    session_manager = SessionManager(page, auth_machine)
    
    return session_manager


def attempt(page: Any, base_url: str, *, timeout_ms: int = 20000) -> tuple[str, str]:
    """Try to sign in once. Returns (status, detail) tuple.

    This is the compatibility wrapper that watcher.py calls.
    It uses the state machine internally but returns the legacy format.
    """
    # Quick credentials check (watcher expects this behavior)
    if credentials() is None:
        return UNKNOWN, "no credentials in .env (AMAZON_LOGIN_EMAIL / AMAZON_LOGIN_PIN)"
    
    # Use real solver for production
    session_manager = create_auth_system(page, use_mock_solver=False)
    
    try:
        success = session_manager.ensure_session(base_url)
        
        if success:
            return OK, "signed in"
        
        # Map internal state to legacy status string
        state = session_manager.auth_machine.state
        
        if state == AuthState.BAD_CREDENTIALS:
            return BAD_CREDENTIALS, "the email or PIN was rejected"
        elif state in (AuthState.CAPTCHA_REQUIRED, AuthState.CAPTCHA_FAILED):
            return CAPTCHA, "a CAPTCHA blocked the login"
        elif state == AuthState.OTP_TIMEOUT:
            return OTP_REQUIRED, "the code was requested but never arrived"
        elif state == AuthState.AUTHENTICATED:
            return OK, "signed in"
        else:
            return UNKNOWN, f"authentication failed at state: {state.name}"
    
    except Exception as exc:
        log.error(f"Authentication error: {exc}")
        return UNKNOWN, f"re-login attempt failed: {str(exc)[:200]}"
"""
Amazon Hiring re-login automation - Refactored with explicit state machine.
"""

from __future__ import annotations

import base64
import logging
import struct
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

OK = "ok"
OTP_REQUIRED = "otp_required"
CAPTCHA = "captcha"
BAD_CREDENTIALS = "bad_credentials"
UNKNOWN = "unknown"


def credentials() -> tuple[str, str] | None:
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
    NONE = auto()
    IMAGE_GRID = auto()
    TOKEN = auto()
    TEXT = auto()
    UNKNOWN = auto()


# ============================================================================
# SELECTORS
# ============================================================================

EMAIL_INPUT = "[data-test-id='input-test-id-login']"
CONTINUE_BUTTON = "[data-test-id='button-continue']"
CONSENT_BUTTON = "[data-test-id='consentBtn']"
SEND_CODE_BUTTON = "[data-test-id='button-submit']"
CODE_INPUT = "[data-test-id='input-test-id-confirmOtp'] input"
VERIFY_BUTTON = "[data-test-id='button-test-id-verifyAccount']"
COUNTRY_TOGGLE = "#country-toggle-button"

PIN_INPUT_SELECTORS = (
    "[data-test-id='input-test-id-pin']",
    "[data-test-id*='pin'] input",
    "input[inputmode='numeric'][maxlength='6']",
)

SUBMIT_BUTTON_SELECTORS = (
    "[data-test-id='button-signIn']",
    "[data-test-id='button-login']",
    "[data-test-id='button-continue']",
)

GRID_IMAGE_SELECTORS = (
    "[data-test-id*='image-grid'] img",
    "[class*='image-grid'] img",
    "[class*='aws-challenge'] img",
    "[class*='challenge'] img",
)

AWS_WAF_FRAME_URL_MARKERS = (
    "edge.sdk.awswaf.com",
    "awswaf.com",
)

AWS_WAF_SHADOW_HOST = "awswaf-captcha"

GRID_CONFIRM_BUTTONS = (
    "button:has-text('Confirm')",
    "button:has-text('Submit')",
    "[data-test-id*='confirm']",
)

COUNTRY_OPTION = "li[role='option']"
COUNTRY_BY_HOST = {
    "hiring.amazon.ca": "Canada",
    "hiring.amazon.com": "United States",
}


# ============================================================================
# STATE DETECTION
# ============================================================================

class StateDetector:
    def __init__(self, page: Any):
        self.page = page

    def _is_visible(self, selector: str) -> bool:
        try:
            elem = self.page.locator(selector).first
            return elem.count() > 0 and elem.is_visible()
        except Exception:
            return False

    def captcha_frame(self):
        try:
            for frame in self.page.frames:
                url = (frame.url or "").lower()
                if any(marker in url for marker in AWS_WAF_FRAME_URL_MARKERS):
                    return frame
        except Exception as exc:
            log.debug("could not enumerate frames: %s", exc)
        return None

    def _frame_text(self, frame: Any) -> str:
        try:
            return (frame.locator("body").inner_text() or "").lower()
        except Exception:
            return ""

    def _get_text(self) -> str:
        parts = []
        try:
            main_text = (self.page.inner_text("body") or "").lower()
            if main_text:
                parts.append(main_text)
        except Exception:
            pass

        try:
            host = self.page.locator(AWS_WAF_SHADOW_HOST).first
            if host.count() > 0:
                shadow_text = host.evaluate(
                    "el => el.shadowRoot ? el.shadowRoot.textContent : ''"
                )
                if shadow_text:
                    parts.append(str(shadow_text).lower())
        except Exception:
            pass

        frame = self.captcha_frame()
        if frame is not None:
            frame_text = self._frame_text(frame)
            if frame_text:
                parts.append(frame_text)

        return "\n".join(parts)

    def detect_state(self) -> AuthState:
        text = self._get_text()

        if self._is_authenticated():
            return AuthState.AUTHENTICATED

        if self._is_bad_credentials(text):
            return AuthState.BAD_CREDENTIALS

        if self._is_otp_entry(text):
            return AuthState.OTP_ENTRY_REQUIRED

        if self.detect_captcha_type() != CaptchaType.NONE:
            return AuthState.CAPTCHA_REQUIRED

        if self._is_otp_send(text):
            return AuthState.OTP_SEND_REQUIRED

        if self._is_pin_required():
            return AuthState.PIN_REQUIRED

        if self._is_email_required():
            return AuthState.EMAIL_REQUIRED

        return AuthState.UNKNOWN_PAGE

    def detect_captcha_type(self) -> CaptchaType:
        try:
            shadow_host = self.page.locator(AWS_WAF_SHADOW_HOST).first
            if shadow_host.count() > 0 and shadow_host.is_visible():
                log.info("AWS WAF shadow host detected (awswaf-captcha)")
                return CaptchaType.IMAGE_GRID
        except Exception:
            pass

        text = self._get_text()
        frame = self.captcha_frame()

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
                if self._is_image_grid(challenge_text, frame=frame):
                    return CaptchaType.IMAGE_GRID
                log.info(
                    "AWS WAF challenge uses image-grid wording but tile markup "
                    "did not match known selectors; routing as IMAGE_GRID"
                )
                return CaptchaType.IMAGE_GRID

            log.info("AWS WAF challenge frame detected but type is unknown: %s", frame.url)
            return CaptchaType.UNKNOWN

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
        )):
            return False

        if "hiring.amazon." in url:
            return True

        if any(marker in text for marker in ("my account", "sign out", "welcome back")):
            return True

        for selector in ("[data-test-id*='dashboard']", "[data-test-id*='user-menu']",
                         "[class*='user-menu']"):
            if self._is_visible(selector):
                return True

        return False

    def _is_bad_credentials(self, text: str) -> bool:
        bad_markers = (
            "incorrect password",
            "incorrect email",
            "does not match",
            "not recognised",
            "not recognized",
            "invalid credentials",
        )
        return any(marker in text for marker in bad_markers)

    def _is_otp_entry(self, text: str) -> bool:
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
        otp_send_markers = (
            "where should we send",
            "send verification code",
            "send code",
        )
        return any(marker in text for marker in otp_send_markers)

    def _is_pin_required(self) -> bool:
        for selector in PIN_INPUT_SELECTORS:
            if self._is_visible(selector):
                return True
        return False

    def _is_email_required(self) -> bool:
        return self._is_visible(EMAIL_INPUT)

    def _is_image_grid(self, text: str, frame: Any = None) -> bool:
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
        selectors = list(GRID_IMAGE_SELECTORS)
        if frame is not None:
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
        return max_visible >= 4

    def _is_token_captcha(self) -> bool:
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
        return "captcha" in text and not self._is_image_grid(text)


# ============================================================================
# CAPTCHA SOLVER INTERFACE
# ============================================================================

class CaptchaSolver:
    def solve(self, page: Any, captcha_type: CaptchaType, frame: Any = None,
              challenge_element: Any = None) -> bool:
        raise NotImplementedError


class MockCaptchaSolver(CaptchaSolver):
    def __init__(self, should_succeed: bool = True):
        self.should_succeed = should_succeed

    def solve(self, page: Any, captcha_type: CaptchaType, frame: Any = None,
              challenge_element: Any = None) -> bool:
        log.info("Mock solving %s (challenge_frame=%s, shadow_host=%s)",
                 captcha_type, frame is not None, challenge_element is not None)
        if self.should_succeed:
            time.sleep(0.5)
            return True
        return False


class TwoCaptchaSolver(CaptchaSolver):
    def __init__(self):
        try:
            from twocaptcha import TwoCaptcha
            api_key = os.getenv("TWOCAPTCHA_API_KEY", "")
            self.solver = TwoCaptcha(api_key) if api_key else None
        except ImportError:
            log.warning("2captcha-python not installed - using no solver")
            self.solver = None
        self._net_capture: Dict[str, Optional[str]] = {
            "key": None, "iv": None, "context": None,
            "challenge_script": None, "captcha_script": None,
        }
        self._capture_installed = False

    def solve(self, page: Any, captcha_type: CaptchaType, frame: Any = None,
              challenge_element: Any = None) -> bool:
        if not self.solver:
            log.error("2Captcha API key not configured or library not installed")
            return False

        self._install_network_capture(page)

        if captcha_type == CaptchaType.IMAGE_GRID and self._is_amazon_waf(page, frame):
            return self._solve_amazon_waf(page, frame, challenge_element)
        if captcha_type == CaptchaType.IMAGE_GRID:
            return self._solve_image_grid(page, frame, challenge_element)
        elif captcha_type == CaptchaType.TOKEN:
            return self._solve_token(page, frame, challenge_element)
        elif captcha_type == CaptchaType.TEXT:
            return self._solve_text(page, frame, challenge_element)
        return False

    def _is_amazon_waf(self, page: Any, frame: Any = None) -> bool:
        try:
            for target in ((frame,) if frame else ()) + (page,):
                url = (target.url or "").lower()
                if any(marker in url for marker in AWS_WAF_FRAME_URL_MARKERS):
                    return True
            shadow_host = page.locator(AWS_WAF_SHADOW_HOST).first
            if shadow_host.count() > 0:
                return True
        except Exception:
            pass
        return False

    def _install_network_capture(self, page: Any) -> None:
        if self._capture_installed:
            return
        self._capture_installed = True

        def on_response(response: Any) -> None:
            try:
                url = response.url or ""
                if not any(m in url for m in ("awswaf", "captcha", "challenge")):
                    return
                body = response.text()
                if body and len(body) < 2_000_000:
                    self._absorb_params(body, self._net_capture)
                if "challenge.js" in url:
                    self._net_capture["challenge_script"] = url
                if "captcha.js" in url or "jsapi.js" in url:
                    self._net_capture["captcha_script"] = url
            except Exception:
                pass

        def on_request(request: Any) -> None:
            try:
                url = request.url or ""
                if "challenge.js" in url:
                    self._net_capture["challenge_script"] = url
                if "captcha.js" in url or "jsapi.js" in url:
                    self._net_capture["captcha_script"] = url
            except Exception:
                pass

        try:
            page.on("response", on_response)
            page.on("request", on_request)
        except Exception as exc:
            log.debug("could not install network capture: %s", exc)

    def _absorb_params(self, blob: str, cap: Dict[str, Optional[str]]) -> None:
        if not cap.get("iv"):
            m = re.search(r'["\']?iv["\']?\s*[:=]\s*["\']([A-Za-z0-9+/=_\-]{8,128})["\']', blob)
            if m: cap["iv"] = m.group(1)
        if not cap.get("context"):
            m = re.search(r'["\']?context["\']?\s*[:=]\s*["\']([A-Za-z0-9+/=_\-]{40,8192})["\']', blob)
            if m: cap["context"] = m.group(1)
        if not cap.get("key"):
            m = re.search(
                r'["\']?(?:apiKey|api_key|websiteKey|website_key|sitekey|key)["\']?\s*[:=]\s*'
                r'["\']([A-Za-z0-9+/=_\-]{20,8192})["\']', blob)
            if m: cap["key"] = m.group(1)

    def _dom_probe(self, page: Any) -> Optional[dict]:
        try:
            return page.evaluate("""() => {
                const out = {globals: {}, elProps: {}, elAttrs: {}, shadowHtml: '', scripts: ''};
                for (const g of ['gokuProps', 'awsWafParams', 'wafParams', 'captchaConfig']) {
                    if (window[g] && typeof window[g] === 'object') out.globals[g] = window[g];
                }
                const el = document.querySelector('awswaf-captcha');
                if (el) {
                    try {
                        out.elAttrs = Object.fromEntries(
                            el.getAttributeNames().map(a => [a, el.getAttribute(a)]));
                    } catch (e) {}
                    try {
                        const props = {};
                        for (const k of Object.keys(el)) {
                            const v = el[k];
                            if (typeof v === 'string' && v.length < 5000) props[k] = v;
                            else if (v && typeof v === 'object') {
                                try { props[k] = JSON.stringify(v); } catch (e) {}
                            }
                        }
                        out.elProps = props;
                    } catch (e) {}
                    try { out.shadowHtml = el.shadowRoot ? el.shadowRoot.innerHTML : ''; } catch (e) {}
                }
                try {
                    out.scripts = Array.from(document.querySelectorAll('script'))
                        .map(s => s.textContent || '').join('\\n');
                } catch (e) {}
                return out;
            }""")
        except Exception as exc:
            log.debug("dom probe failed: %s", exc)
            return None

    def _collect_params(self, page: Any, frame: Any = None) -> Dict[str, Optional[str]]:
        cap = dict(self._net_capture)
        probe = self._dom_probe(page)
        blobs = []
        if probe:
            for g in (probe.get("globals") or {}).values():
                if isinstance(g, dict):
                    if not cap.get("key") and isinstance(g.get("key"), str): cap["key"] = g["key"]
                    if not cap.get("key") and isinstance(g.get("apiKey"), str): cap["key"] = g["apiKey"]
                    if not cap.get("iv") and isinstance(g.get("iv"), str): cap["iv"] = g["iv"]
                    if not cap.get("context") and isinstance(g.get("context"), str): cap["context"] = g["context"]
            for src in (probe.get("elProps") or {}, probe.get("elAttrs") or {}):
                for k, v in src.items():
                    if not isinstance(v, str): continue
                    lk = k.lower()
                    if not cap.get("iv") and lk == "iv": cap["iv"] = v
                    if not cap.get("context") and lk == "context": cap["context"] = v
                    if not cap.get("key") and lk in ("key", "apikey", "websitekey", "sitekey"): cap["key"] = v
                    if "{" in v and '"iv"' in v: self._absorb_params(v, cap)
            blobs.append(probe.get("shadowHtml") or "")
            blobs.append(probe.get("scripts") or "")
        if frame is not None:
            try:
                blobs.append(frame.evaluate(
                    "() => document.documentElement ? document.documentElement.innerHTML : ''") or "")
            except Exception: pass
        for blob in blobs:
            if blob: self._absorb_params(blob, cap)
        return cap

    def _solve_amazon_waf(self, page: Any, frame: Any = None,
                          challenge_element: Any = None) -> bool:
        log.info("Solving Amazon WAF CAPTCHA with 2Captcha amazon_waf method")

        if challenge_element is None:
            try:
                challenge_element = page.locator(AWS_WAF_SHADOW_HOST).first
                if challenge_element.count() == 0: challenge_element = None
            except Exception: challenge_element = None

        page.wait_for_timeout(2000)
        cap = self._collect_params(page, frame)

        if not (cap.get("key") and cap.get("iv") and cap.get("context")) and challenge_element:
            log.info("params incomplete; clicking refresh to force a new challenge fetch")
            self._click_refresh(challenge_element)
            deadline = time.time() + 10
            while time.time() < deadline:
                time.sleep(1.0)
                cap = self._collect_params(page, frame)
                if cap.get("key") and cap.get("iv") and cap.get("context"):
                    break

        if cap.get("key") and cap.get("iv") and cap.get("context"):
            log.info("WAF params found (key=%.20s... iv=%.16s... context=%.16s...)",
                     cap["key"], cap["iv"], cap["context"])
            if self._solve_via_token(page, cap): return True
            log.warning("token route failed; falling back to interactive grid")
        else:
            log.warning("could not extract key/iv/context from any source; falling back to interactive grid")
            try:
                if challenge_element:
                    shadow_html = challenge_element.evaluate("el => el.shadowRoot ? el.shadowRoot.innerHTML : 'no shadow root'")
                    log.info("DEBUG: shadow root innerHTML length=%d, preview=%s", len(shadow_html), shadow_html[:1500])
                    iframes = challenge_element.evaluate("""el => {
                        if (!el.shadowRoot) return [];
                        return Array.from(el.shadowRoot.querySelectorAll('iframe')).map(f => f.src);
                    }""")
                    log.info("DEBUG: iframes inside shadow root: %s", iframes)
                scripts_text = page.evaluate("""() => {
                    return Array.from(document.querySelectorAll('script')).map(s => s.innerText).join('\\n---\\n');
                }""")
                log.info("DEBUG: main page scripts (first 2000 chars): %s", scripts_text[:2000])
            except Exception as exc:
                log.error("DEBUG dump failed: %s", exc)

        return self._solve_image_grid(page, frame, challenge_element)

    def _solve_via_token(self, page: Any, cap: Dict[str, Optional[str]]) -> bool:
        try:
            kwargs: Dict[str, Any] = dict(
                sitekey=cap["key"], iv=cap["iv"], context=cap.get("context", ""),
                url=page.url, timeout=120)
            if cap.get("challenge_script"): kwargs["challenge_script"] = cap["challenge_script"]
            if cap.get("captcha_script"): kwargs["captcha_script"] = cap["captcha_script"]

            result = self.solver.amazon_waf(**kwargs)
            log.info("2Captcha amazon_waf raw result: %s", str(result)[:300])

            if isinstance(result, dict):
                token = (result.get("captcha_voucher") or result.get("code") or result.get("existing_token"))
            elif hasattr(result, "code"): token = result.code
            else: token = str(result)

            if not token:
                log.error("2Captcha returned no token")
                return False

            injected = page.evaluate("""
                (token) => {
                    const tries = [];
                    if (window.AwsWafCaptcha && typeof window.AwsWafCaptcha.submitCaptcha === 'function') {
                        window.AwsWafCaptcha.submitCaptcha(token); tries.push('AwsWafCaptcha');
                    }
                    if (window.ChallengeScript && typeof window.ChallengeScript.submitCaptcha === 'function') {
                        window.ChallengeScript.submitCaptcha(token); tries.push('ChallengeScript');
                    }
                    const el = document.querySelector('awswaf-captcha');
                    if (el) {
                        if (typeof el.submitCaptcha === 'function') { el.submitCaptcha(token); tries.push('element'); }
                        else if (typeof el.onCaptchaSolved === 'function') { el.onCaptchaSolved({token: token}); tries.push('onCaptchaSolved'); }
                        else {
                            try { el.token = token; el.value = token; } catch (e) {}
                            el.dispatchEvent(new CustomEvent('captcha-solved', {detail: {token: token}}));
                            tries.push('custom-event');
                        }
                    }
                    document.cookie = "aws-waf-token=" + token + "; path=/";
                    return tries.join(',') || 'cookie_only';
                }
            """, token)
            log.info("token injection result: %s", injected)
            time.sleep(2.0)
            return True
        except Exception as exc:
            log.error("amazon_waf token route failed: %s", exc)
            return False

    def _click_refresh(self, shadow_host: Any) -> None:
        try:
            shadow_host.evaluate("""(host) => {
                const root = host.shadowRoot; if (!root) return false;
                const nodes = root.querySelectorAll('button, [role="button"], svg, div, span');
                for (const el of nodes) {
                    const hint = ((el.getAttribute && (el.getAttribute('aria-label') || '')) + ' ' +
                                  ((el.getAttribute && el.getAttribute('id')) || '') + ' ' +
                                  ((el.className || '').toString())).toLowerCase();
                    if (hint.includes('refresh') || hint.includes('retry') || hint.includes('reload')) {
                        el.dispatchEvent(new MouseEvent('click', {bubbles: true, composed: true}));
                        return true;
                    }
                }
                return false;
            }""")
        except Exception as exc:
            log.debug("refresh click failed: %s", exc)

    def _captcha_gone(self, page: Any) -> bool:
        try:
            host = page.locator(AWS_WAF_SHADOW_HOST).first
            return host.count() == 0 or not host.is_visible()
        except Exception:
            return True

    def _get_instruction(self, page, shadow_host) -> str:
        try:
            shadow_text = shadow_host.evaluate(
                "el => el.shadowRoot ? el.shadowRoot.textContent : ''"
            )
            if not shadow_text: return ""
            text = str(shadow_text).lower()
            for marker in ("choose all the", "select all", "choose all"):
                idx = text.find(marker)
                if idx != -1:
                    start = max(0, idx - 20)
                    end = min(len(text), idx + 80)
                    return text[start:end].strip()
            return ""
        except Exception as exc:
            log.debug("Could not read instruction: %s", exc)
            return ""

    def _get_png_size(self, png_bytes: bytes) -> tuple[int, int]:
        try:
            if png_bytes[:8] != b'\x89PNG\r\n\x1a\n': return 0, 0
            width = struct.unpack('>I', png_bytes[16:20])[0]
            height = struct.unpack('>I', png_bytes[20:24])[0]
            return width, height
        except Exception as exc:
            log.debug(f"Could not parse PNG size: {exc}")
            return 0, 0

    def _parse_coordinates(self, coords_str: str) -> list[tuple[float, float]]:
        coords_str = coords_str.strip()
        points = []
        if coords_str.startswith("coordinates:"):
            coords_str = coords_str[len("coordinates:"):].strip()

        if coords_str.startswith("["):
            import json
            try:
                data = json.loads(coords_str)
                for item in data:
                    if isinstance(item, (list, tuple)) and len(item) == 2:
                        points.append((float(item[0]), float(item[1])))
                if points: return points
            except Exception: pass

        if "x=" in coords_str and "y=" in coords_str:
            for part in re.split(r"[;|]", coords_str):
                part = part.strip()
                if not part: continue
                x_match = re.search(r"x\s*=\s*([\d.]+)", part)
                y_match = re.search(r"y\s*=\s*([\d.]+)", part)
                if x_match and y_match:
                    points.append((float(x_match.group(1)), float(y_match.group(1))))
            if points: return points

        if "|" in coords_str:
            for part in coords_str.split("|"):
                part = part.strip()
                if "," in part:
                    x, y = part.split(",", 1)
                    try: points.append((float(x), float(y)))
                    except ValueError: continue
            if points: return points

        nums = [n.strip() for n in coords_str.split(",")]
        nums = [n for n in nums if n]
        if len(nums) >= 2 and len(nums) % 2 == 0:
            for i in range(0, len(nums), 2):
                try: points.append((float(nums[i]), float(nums[i + 1])))
                except ValueError: continue
        return points

    def _solve_image_grid(self, page: Any, frame: Any = None,
                          challenge_element: Any = None) -> bool:
        log.info("Solving image grid CAPTCHA with 2Captcha (page-level visual clicking)")

        if challenge_element is None:
            challenge_element = page.locator(AWS_WAF_SHADOW_HOST).first
            
        page.wait_for_timeout(2000)

        for attempt in (1, 2, 3):
            if self._captcha_gone(page):
                log.info("captcha widget cleared before attempt %d", attempt)
                return True

            instruction = self._get_instruction(page, challenge_element)
            if not instruction: instruction = "Select all images that match the instruction"

            try:
                img_bytes = challenge_element.screenshot()
                w, h = self._get_png_size(img_bytes)
                if w <= 0 or h <= 0: w, h = 320, 320
                img_base64 = base64.b64encode(img_bytes).decode("utf-8")
            except Exception as exc:
                log.error("Failed to screenshot CAPTCHA element: %s", exc)
                return False

            try:
                log.info("attempt %d: sending element screenshot to 2Captcha (coordinates)", attempt)
                result = self.solver.coordinates(
                    img_base64, textinstructions=instruction, timeout=120,
                )
                coordinates_str = result.get("code") or ""
                log.info("received coordinates: %s", coordinates_str[:120])
            except Exception as exc:
                log.error("2Captcha coordinates request failed: %s", exc)
                return False

            points = self._parse_coordinates(coordinates_str)
            if not points:
                log.error("Could not parse coordinates: %s", coordinates_str[:200])
                return False

            try:
                box = challenge_element.bounding_box()
                if not box:
                    log.error("Could not get bounding box for shadow host")
                    return False
            except Exception as exc:
                log.error("bounding_box() failed: %s", exc)
                return False
                
            log.info("shadow host bounding box: %s, png size: %dx%d", box, w, h)

            for x, y in points:
                # scale 2Captcha coordinates (which are in image pixels) to CSS pixels
                css_x = x * (box["width"] / w)
                css_y = y * (box["height"] / h)
                abs_x = box["x"] + css_x
                abs_y = box["y"] + css_y
                log.info("clicking at absolute coords (%.0f, %.0f)", abs_x, abs_y)
                try:
                    page.mouse.click(abs_x, abs_y)
                    time.sleep(0.3)
                except Exception as exc:
                    log.error("mouse.click failed: %s", exc)
                    return False

            time.sleep(1.5)

            if not self._click_confirm_js_or_visual(page, challenge_element):
                log.error("Could not click Confirm button")
                continue

            log.info("Confirm clicked (attempt %d) - awaiting state transition", attempt)
            deadline = time.time() + 15
            while time.time() < deadline:
                if self._captcha_gone(page):
                    log.info("captcha widget cleared")
                    return True
                time.sleep(0.5)

            log.warning("attempt %d did not clear the captcha", attempt)
            self._click_refresh_visual(page, challenge_element)
            page.wait_for_timeout(2000)

        return self._captcha_gone(page)

    def _click_confirm_js_or_visual(self, page: Any, shadow_host: Any) -> bool:
        clicked_js = shadow_host.evaluate("""(host) => {
            const root = host.shadowRoot; if (!root) return false;
            const candidates = root.querySelectorAll('button, [role="button"]');
            for (const el of candidates) {
                const text = (el.innerText || el.textContent || '').toLowerCase();
                if (text.includes('confirm') || text.includes('verify') || text.includes('submit')) {
                    el.dispatchEvent(new MouseEvent('click', {bubbles: true, composed: true, cancelable: true}));
                    return true;
                }
            }
            return false;
        }""")
        if clicked_js:
            log.info("Confirm clicked via JS")
            return True
            
        log.info("JS confirm failed, trying visual confirm click")
        try:
            img_bytes = shadow_host.screenshot()
            w, h = self._get_png_size(img_bytes)
            if w <= 0 or h <= 0: w, h = 320, 320
            img_base64 = base64.b64encode(img_bytes).decode("utf-8")
            result = self.solver.coordinates(
                img_base64, textinstructions="Click the button that says 'Confirm' or 'Verify' or 'Submit'", timeout=60,
            )
            coords_str = result.get("code") or ""
            points = self._parse_coordinates(coords_str)
            if points:
                box = shadow_host.bounding_box()
                if box:
                    x, y = points[0]
                    css_x = x * (box["width"] / w)
                    css_y = y * (box["height"] / h)
                    page.mouse.click(box["x"] + css_x, box["y"] + css_y)
                    log.info("Confirm clicked via visual coordinates")
                    return True
        except Exception as exc:
            log.error("visual confirm click failed: %s", exc)
            
        return False

    def _click_refresh_visual(self, page: Any, shadow_host: Any) -> None:
        try:
            img_bytes = shadow_host.screenshot()
            w, h = self._get_png_size(img_bytes)
            if w <= 0 or h <= 0: w, h = 320, 320
            img_base64 = base64.b64encode(img_bytes).decode("utf-8")
            result = self.solver.coordinates(
                img_base64, textinstructions="Click the refresh or reload icon", timeout=30,
            )
            coords_str = result.get("code") or ""
            points = self._parse_coordinates(coords_str)
            if points:
                box = shadow_host.bounding_box()
                if box:
                    x, y = points[0]
                    css_x = x * (box["width"] / w)
                    css_y = y * (box["height"] / h)
                    page.mouse.click(box["x"] + css_x, box["y"] + css_y)
                    log.info("Refresh clicked via visual coordinates")
                    return
        except Exception:
            pass
        self._click_refresh(shadow_host)

    def _click_confirm(self, shadow_host) -> bool: return False
    def _click_confirm_by_id(self, shadow_host) -> bool: return False
    def _find_grid(self, shadow_host): return None
    def _solve_token(self, page, frame=None, challenge_element=None) -> bool: return False
    def _solve_text(self, page, frame=None, challenge_element=None) -> bool: return False


# ============================================================================
# AUTHENTICATION STATE MACHINE
# ============================================================================

class AuthenticationStateMachine:
    def __init__(self, page: Any, solver: CaptchaSolver):
        self.page = page
        self.solver = solver
        self.detector = StateDetector(page)
        self.state = AuthState.LOGIN_PAGE
        self.otp_requested_at = None
        self.country = "Canada"

    def run(self, base_url: str) -> AuthState:
        self._log_transition("AUTH_START")
        self.country = self._country_for(base_url)
        log.info("logging in as %s (from %s)", self.country, base_url)

        try:
            self.page.goto(base_url.rstrip("/") + "/app#/jobSearch",
                           wait_until="domcontentloaded")
            self.page.wait_for_timeout(2000)
        except Exception as exc:
            log.debug("could not set the country context: %s", exc)

        self.page.goto("https://auth.hiring.amazon.com/#/login")
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

    def _transition_to_next(self) -> bool:
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
        try:
            consent = self.page.locator(CONSENT_BUTTON).first
            if consent.count() and consent.is_visible():
                consent.click(timeout=5000)
                self.page.wait_for_timeout(500)
        except Exception:
            pass

    def _select_country(self, country: str) -> bool:
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
        for host, country in COUNTRY_BY_HOST.items():
            if host in (base_url or ""):
                return country
        return "Canada"

    def _submit_email(self) -> bool:
        email = os.getenv("AMAZON_LOGIN_EMAIL", "")
        if not email:
            log.error("AMAZON_LOGIN_EMAIL not configured")
            return False
        try:
            country = getattr(self, "country", None) or self._country_for(self.page.url)
            if not self._select_country(country):
                log.warning("Could not select country, continuing anyway")
            email_input = self.page.locator(EMAIL_INPUT).first
            email_input.wait_for(state="visible", timeout=10000)
            email_input.fill(email)
            self.page.locator(CONTINUE_BUTTON).first.click(timeout=10000)
            self._log_transition("EMAIL_SUBMITTED")
            return self._wait_for_state([AuthState.PIN_REQUIRED, AuthState.CAPTCHA_REQUIRED])
        except Exception as exc:
            log.error(f"Email submission failed: {exc}")
            return False

    def _submit_pin(self) -> bool:
        pin = os.getenv("AMAZON_LOGIN_PIN", "")
        if not pin:
            log.error("AMAZON_LOGIN_PIN not configured")
            return False
        try:
            pin_input = None
            for selector in PIN_INPUT_SELECTORS:
                if self.detector._is_visible(selector):
                    pin_input = self.page.locator(selector).first
                    break
            if not pin_input:
                log.error("PIN input not found")
                return False
            pin_input.fill(pin)
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
        captcha_type = self.detector.detect_captcha_type()
        self._log_transition(f"CAPTCHA_DETECTED:{captcha_type.name}")

        challenge_frame = self.detector.captcha_frame()
        challenge_element = None
        if challenge_frame is None:
            try:
                challenge_element = self.page.locator(AWS_WAF_SHADOW_HOST).first
                if challenge_element.count() == 0:
                    challenge_element = None
            except Exception:
                pass

        if challenge_frame is not None:
            log.info("CAPTCHA challenge frame: %s", challenge_frame.url)
        elif challenge_element is not None:
            log.info("CAPTCHA shadow host: awswaf-captcha")

        success = self.solver.solve(
            self.page,
            captcha_type,
            frame=challenge_frame,
            challenge_element=challenge_element,
        )

        if not success:
            self._log_transition("CAPTCHA_FAILED")
            return False

        moved_forward = self._wait_for_state([
            AuthState.OTP_SEND_REQUIRED,
            AuthState.OTP_ENTRY_REQUIRED,
            AuthState.AUTHENTICATED,
        ], timeout_ms=30000)

        if not moved_forward:
            self._log_transition("CAPTCHA_FAILED:NO_STATE_TRANSITION")
            return False

        self._log_transition("CAPTCHA_COMPLETED")
        return True

    def _request_otp(self) -> bool:
        if not self.detector._is_visible(SEND_CODE_BUTTON):
            log.warning("Send code button is not visible")
            return False

        self.page.locator(SEND_CODE_BUTTON).first.click()
        self.otp_requested_at = time.time()
        self._log_transition("OTP_REQUESTED")

        deadline = time.time() + 60
        while time.time() < deadline:
            text = self.detector._get_text()
            if self.detector._is_otp_entry(text):
                self._log_transition("OTP_ENTRY_REQUIRED")
                return True

            captype = self.detector.detect_captcha_type()
            if captype != CaptchaType.NONE:
                self._log_transition(f"CAPTCHA_DETECTED:{captype.name}")
                return True

            time.sleep(0.5)

        log.warning("No OTP entry or CAPTCHA appeared within 60 seconds")
        return False

    def _submit_otp(self) -> bool:
        if not self.otp_requested_at:
            return False
        code = otp_mail.fetch_code(self.otp_requested_at)
        if not code:
            self._log_transition("OTP_TIMEOUT")
            return False
        self._log_transition("OTP_RECEIVED")
        try:
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
        log.info(f"AUTH_STATE: {transition}")


# ============================================================================
# SESSION MANAGER
# ============================================================================

class SessionManager:
    def __init__(self, page: Any, auth_machine: AuthenticationStateMachine):
        self.page = page
        self.auth_machine = auth_machine
        self.detector = StateDetector(page)

    def ensure_session(self, base_url: str) -> bool:
        if self._is_session_valid():
            log.info("SESSION_READY")
            return True
        result = self.auth_machine.run(base_url)
        if result == AuthState.AUTHENTICATED:
            self._persist_session()
            log.info("SESSION_READY")
            return True
        return False

    def _is_session_valid(self) -> bool:
        return self.detector.detect_state() == AuthState.AUTHENTICATED

    def _persist_session(self):
        pass


# ============================================================================
# MAIN ENTRY POINT
# ============================================================================

def create_auth_system(page: Any, use_mock_solver: bool = True) -> SessionManager:
    solver = MockCaptchaSolver() if use_mock_solver else TwoCaptchaSolver()
    auth_machine = AuthenticationStateMachine(page, solver)
    session_manager = SessionManager(page, auth_machine)
    return session_manager


def attempt(page: Any, base_url: str, *, timeout_ms: int = 20000) -> tuple[str, str]:
    if credentials() is None:
        return UNKNOWN, "no credentials in .env (AMAZON_LOGIN_EMAIL / AMAZON_LOGIN_PIN)"
    session_manager = create_auth_system(page, use_mock_solver=False)
    try:
        success = session_manager.ensure_session(base_url)
        if success:
            return OK, "signed in"
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
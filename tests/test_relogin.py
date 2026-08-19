"""Tests for the re-login state machine.

Deliberately scoped: this covers state DETECTION, credential handling, the
selectors, and the contract watcher.py depends on. It does not test the
CAPTCHA solvers — those are the module author's, and they need a live
challenge and a paid API to exercise meaningfully.

No browser and no network: every page here is a fake.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import relogin
from relogin import AuthState, CaptchaType, StateDetector


class FakePage:
    """A page that says what it was told to say."""

    def __init__(self, text: str = "", url: str = "https://auth.hiring.amazon.com/#/login",
                 visible: tuple[str, ...] = ()):
        self.text = text
        self.url = url
        self.visible = visible
        self.filled: list[tuple[str, str]] = []
        self.clicked: list[str] = []

    def inner_text(self, _selector):
        return self.text

    def locator(self, selector):
        page = self

        class L:
            @property
            def first(self_inner):
                return self_inner

            def count(self_inner):
                return 1 if any(v in selector for v in page.visible) else 0

            def is_visible(self_inner):
                return any(v in selector for v in page.visible)

            def fill(self_inner, value):
                page.filled.append((selector, value))

            def click(self_inner, **_kw):
                page.clicked.append(selector)

            def wait_for(self_inner, **_kw):
                if not any(v in selector for v in page.visible):
                    raise RuntimeError(f"not visible: {selector}")

        return L()


# ── the contract watcher.py depends on ──────────────────────────────────────
def test_the_status_constants_are_strings():
    """watcher.py compares against these by value, so they must be strings
    and must not collide."""
    values = [relogin.OK, relogin.OTP_REQUIRED, relogin.CAPTCHA,
              relogin.BAD_CREDENTIALS, relogin.UNKNOWN]
    assert all(isinstance(v, str) for v in values)
    assert len(set(values)) == len(values), "each status must be distinct"


def test_attempt_takes_the_arguments_the_watcher_passes():
    import inspect

    params = list(inspect.signature(relogin.attempt).parameters)
    assert params[:2] == ["page", "base_url"]


def test_credentials_reads_the_environment(monkeypatch):
    monkeypatch.setenv("AMAZON_LOGIN_EMAIL", "someone@example.com")
    monkeypatch.setenv("AMAZON_LOGIN_PIN", "123456")
    assert relogin.credentials() == ("someone@example.com", "123456")


def test_credentials_is_none_when_unset(monkeypatch):
    monkeypatch.delenv("AMAZON_LOGIN_EMAIL", raising=False)
    monkeypatch.delenv("AMAZON_LOGIN_PIN", raising=False)
    monkeypatch.delenv("AMAZON_LOGIN_PASSWORD", raising=False)
    assert relogin.credentials() is None


def test_a_pin_that_is_not_six_digits_warns(monkeypatch, caplog):
    monkeypatch.setenv("AMAZON_LOGIN_EMAIL", "someone@example.com")
    monkeypatch.setenv("AMAZON_LOGIN_PIN", "letmein")
    with caplog.at_level("WARNING"):
        relogin.credentials()
    assert "6-digit PIN" in caplog.text


# ── the selector that cost a login ──────────────────────────────────────────
def test_the_code_selector_targets_an_input_not_its_wrapper():
    """Regression. data-test-id='input-test-id-confirmOtp' is a <div> wrapping
    the field, and filling it raises "Element is not an <input>" — after the
    code has already been fetched from the inbox, which is the expensive part."""
    assert relogin.CODE_INPUT.strip().endswith("input")


# ── state detection ─────────────────────────────────────────────────────────
def test_a_url_alone_no_longer_counts_as_authenticated():
    """The old check returned True for any URL containing "/app" — which it
    does throughout the login flow, before anything is submitted. Four
    consecutive re-logins reported success 22-162ms after pressing Continue,
    faster than a page can load, while the session stayed dead for 25 hours.
    A URL is not evidence."""
    page = FakePage(url="https://hiring.amazon.ca/app#/jobSearch")
    # With nothing on the page to prove a session, this must not claim one.
    assert StateDetector(page).detect_state() is not AuthState.AUTHENTICATED


def test_still_on_the_auth_domain_is_never_authenticated():
    page = FakePage(url="https://auth.hiring.amazon.com/#/login")
    assert StateDetector(page)._is_authenticated() is False


def test_rejected_credentials_are_detected():
    page = FakePage(text="the password you entered is incorrect password")
    assert StateDetector(page).detect_state() is AuthState.BAD_CREDENTIALS


def test_a_captcha_outranks_the_other_states():
    """A challenge must be reported as a challenge even when the code field is
    also on screen — otherwise the flow waits for mail that is never sent."""
    page = FakePage(text="let's confirm you are human choose all the clocks")
    assert StateDetector(page).detect_state() is AuthState.CAPTCHA_REQUIRED


def test_the_code_entry_screen_is_detected():
    page = FakePage(text="enter the verification code sent to t****n@gmail.com")
    assert StateDetector(page).detect_state() is AuthState.OTP_ENTRY_REQUIRED


def test_an_unknown_page_is_not_guessed_at():
    page = FakePage(text="something else entirely", url="https://example.com")
    assert StateDetector(page).detect_state() is AuthState.UNKNOWN_PAGE


def test_captcha_type_is_none_on_an_ordinary_page():
    page = FakePage(text="enter your personal pin")
    assert StateDetector(page).detect_captcha_type() is CaptchaType.NONE


# ── failure handling ────────────────────────────────────────────────────────
def test_attempt_without_credentials_reports_rather_than_raises(monkeypatch):
    monkeypatch.delenv("AMAZON_LOGIN_EMAIL", raising=False)
    monkeypatch.delenv("AMAZON_LOGIN_PIN", raising=False)
    monkeypatch.delenv("AMAZON_LOGIN_PASSWORD", raising=False)
    status, detail = relogin.attempt(FakePage(), "https://hiring.amazon.ca")
    assert status == relogin.UNKNOWN
    assert isinstance(detail, str) and detail


def test_attempt_never_raises_on_a_broken_page(monkeypatch):
    """A failed re-login must leave the watcher detecting and alerting."""
    monkeypatch.setenv("AMAZON_LOGIN_EMAIL", "someone@example.com")
    monkeypatch.setenv("AMAZON_LOGIN_PIN", "123456")

    class Exploding:
        url = ""

        def goto(self, *a, **kw):
            raise RuntimeError("network is down")

        def wait_for_timeout(self, _ms):
            pass

        def inner_text(self, _s):
            raise RuntimeError("no page")

        def locator(self, _s):
            raise RuntimeError("no page")

    status, detail = relogin.attempt(Exploding(), "https://hiring.amazon.ca")
    assert status in (relogin.UNKNOWN, relogin.OTP_REQUIRED, relogin.CAPTCHA)
    assert isinstance(detail, str)


# ── entering the code: typed, not filled ────────────────────────────────────
class CodeFieldPage:
    """A Stencil-style field: fill() sets nothing the component believes."""

    def __init__(self, honours_fill: bool = False, button_text: str = "Continue"):
        self.honours_fill = honours_fill
        self.button_text = button_text
        self.value = ""
        self.typed = ""
        self.clicked: list[str] = []
        self.url = "https://auth.hiring.amazon.com/#/login"

    def wait_for_timeout(self, _ms):
        pass

    def locator(self, selector):
        page = self

        class L:
            def __init__(self, sel):
                self.sel = sel

            @property
            def first(self):
                return self

            def count(self):
                if "maxlength='1'" in self.sel:
                    return 0                      # not the six-box layout
                if "input" in self.sel or "confirmOtp" in self.sel:
                    return 1
                return 1 if page.button_text.lower() in self.sel.lower() else 0

            def is_visible(self):
                return self.count() > 0

            def click(self, **_kw):
                page.clicked.append(self.sel)

            def fill(self, value):
                if page.honours_fill or value == "":
                    page.value = value

            def press_sequentially(self, text, **_kw):
                page.typed += text
                page.value = page.typed          # keystrokes the component sees

            def input_value(self):
                return page.value

            def wait_for(self, **_kw):
                pass

        return L(selector)


def test_the_code_is_typed_so_the_component_registers_it():
    """From a failure screenshot: fill() put 832013 in the box, the component
    still showed it greyed with an invalid icon, and Continue did nothing."""
    import login_flow

    page = CodeFieldPage(honours_fill=False)
    assert login_flow.enter_code(page, "832013") is True
    assert page.typed == "832013", "must use key events, not fill()"
    assert page.value == "832013", "and must read back what it typed"


def test_a_masked_field_is_not_treated_as_a_failure():
    """Corrected by the account holder: this component renders the code greyed
    with a circle-slash icon on EVERY account — normal styling, not an invalid
    state — and may not report its value back. Refusing to submit on that
    basis would fail a login that was about to succeed."""
    import login_flow

    class SilentField:
        def locator(self, selector):
            class L:
                @property
                def first(self_inner):
                    return self_inner

                def count(self_inner):
                    return 0 if "maxlength='1'" in selector else 1

                def is_visible(self_inner):
                    return True

                def click(self_inner, **_kw):
                    pass

                def fill(self_inner, _value):
                    pass

                def press_sequentially(self_inner, _text, **_kw):
                    pass

                def input_value(self_inner):
                    return ""          # nothing ever lands

                def wait_for(self_inner, **_kw):
                    pass

            return L()

    assert login_flow.enter_code(SilentField(), "832013") is True


def test_submit_finds_the_button_whichever_word_it_uses():
    """Confirmed twice, days apart: the same screen offered "Verify" with a
    test-id, then "Continue" with none."""
    import login_flow

    for word in ("Verify", "Continue"):
        page = CodeFieldPage(button_text=word)
        assert login_flow.submit_code(page, presses=1) is True, word
        assert any(word.lower() in c.lower() for c in page.clicked), word


def test_submit_presses_more_than_once_because_the_form_needs_it():
    """Confirmed by the account holder: enter the code, press Verify, then
    press Continue as well. One press leaves you on the same screen looking
    exactly like a rejected code."""
    import login_flow

    page = CodeFieldPage(button_text="Continue")
    login_flow.submit_code(page, presses=3)
    assert len(page.clicked) >= 2, page.clicked


def test_submit_stops_once_the_auth_domain_is_behind_us():
    """It must not keep clicking at a page that has already signed in."""
    import login_flow

    page = CodeFieldPage(button_text="Continue")
    page.url = "https://hiring.amazon.ca/app#/jobSearch"
    assert login_flow.submit_code(page, presses=3) is False
    assert page.clicked == []


def test_state_detection_never_navigates():
    """A state detector must not move the page it is detecting. Asking the
    portal meant navigating, detect_state() calls this constantly, and it
    sailed away from the login form mid-flow — reporting SESSION_READY in 4.6
    seconds without ever submitting an email."""
    navigations = []

    class TrackingPage(FakePage):
        def goto(self, url, **_kw):
            navigations.append(url)

    page = TrackingPage(text="welcome back kamaldeep", url="https://hiring.amazon.ca/app")
    StateDetector(page).detect_state()
    assert navigations == [], f"detect_state navigated to {navigations}"


def test_a_visible_login_form_means_not_authenticated():
    """Off the auth domain with a PIN box on screen is mid-login, not done."""
    page = FakePage(
        text="enter your personal pin",
        url="https://hiring.amazon.ca/app#/jobSearch",
    )
    assert StateDetector(page)._is_authenticated() is False


def test_the_account_menu_counts_as_signed_in():
    page = FakePage(text="my account sign out", url="https://hiring.amazon.ca/")
    assert StateDetector(page)._is_authenticated() is True


def test_no_evidence_either_way_reports_not_authenticated():
    """A false yes stops the login flow dead; a false no merely repeats it."""
    page = FakePage(text="fraud warning: amazon never requests payment",
                    url="https://hiring.amazon.ca/")
    assert StateDetector(page)._is_authenticated() is False


def test_the_login_sets_the_country_context_first():
    """auth.hiring.amazon.com serves both countries. Arrived at cold it returns
    you to the US site — the post-login page carried Amazon's own banner:
    "Seems like you're visiting the US website from Canada." A session
    established there is no use to a watcher polling hiring.amazon.ca."""
    import relogin as module

    visited = []

    class Recording:
        url = "https://hiring.amazon.ca/app#/jobSearch"

        def goto(self, url, **_kw):
            visited.append(url)

        def wait_for_timeout(self, _ms):
            pass

        def inner_text(self, _sel):
            return ""

        def locator(self, _sel):
            class L:
                @property
                def first(self_inner):
                    return self_inner

                def count(self_inner):
                    return 0

                def is_visible(self_inner):
                    return False

                def click(self_inner, **_kw):
                    pass

            return L()

        def keyboard_press(self, _k):
            pass

    machine = module.AuthenticationStateMachine(Recording(), module.MockCaptchaSolver())
    try:
        machine.run("https://hiring.amazon.ca")
    except Exception:
        pass  # the flow will not complete against a stub; only the order matters

    assert visited, "the login navigated nowhere"
    assert "hiring.amazon.ca" in visited[0], (
        f"the country site must be loaded before the auth domain, got {visited[0]}"
    )
    assert any("auth.hiring.amazon" in url for url in visited)


def test_the_country_comes_from_the_site_being_watched():
    """THE bug that kept the Canadian session dead for 26 hours. The country
    was read from self.page.url at the moment the form was filled — and by
    then the page is always auth.hiring.amazon.COM, so the lookup returned
    "United States" every time. Every re-login signed in to the American site
    while the watcher polled the Canadian one."""
    import relogin as module

    machine = module.AuthenticationStateMachine.__new__(module.AuthenticationStateMachine)
    assert machine._country_for("https://hiring.amazon.ca") == "Canada"
    assert machine._country_for("https://hiring.amazon.com") == "United States"

    # The trap: the auth domain is .com whichever country you are logging in to
    assert machine._country_for("https://auth.hiring.amazon.com/#/login") == "United States"


def test_the_run_fixes_the_country_before_touching_the_login_page():
    import inspect

    import relogin as module

    source = inspect.getsource(module.AuthenticationStateMachine.run)
    assert "self.country = self._country_for(base_url)" in source, (
        "the country must be decided from base_url at the start of the run"
    )
    submit = inspect.getsource(module.AuthenticationStateMachine._submit_email)
    assert "self._country_for(self.page.url)" not in submit.split("getattr")[0], (
        "the country must not be re-derived from the current page"
    )


def test_pressing_send_accepts_a_challenge_as_an_outcome():
    """Amazon frequently answers "Send verification code" with a challenge
    rather than the code screen. Waiting only for OTP_ENTRY_REQUIRED spent
    twenty seconds watching for a screen that cannot appear until the
    challenge is cleared, then timed out — without ever asking detect_state(),
    so the solver was never invoked."""
    import inspect

    import relogin as module

    source = inspect.getsource(module.AuthenticationStateMachine._request_otp)
    assert "AuthState.CAPTCHA_REQUIRED" in source, (
        "a challenge after Send must end the wait, not time it out"
    )
    assert "AuthState.OTP_ENTRY_REQUIRED" in source


def test_a_captcha_state_routes_to_the_solver():
    """The routing existed all along; nothing ever reached it."""
    import inspect

    import relogin as module

    source = inspect.getsource(module.AuthenticationStateMachine._transition_to_next)
    assert "CAPTCHA_REQUIRED" in source and "_solve_captcha" in source

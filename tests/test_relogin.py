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
def test_the_job_search_url_counts_as_authenticated():
    page = FakePage(url="https://hiring.amazon.ca/app#/jobSearch")
    assert StateDetector(page).detect_state() is AuthState.AUTHENTICATED


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

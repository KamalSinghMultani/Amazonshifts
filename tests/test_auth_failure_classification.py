from __future__ import annotations

from types import SimpleNamespace

import session_refresh


class FakeDetector:
    def detect_captcha_type(self):
        return session_refresh.login_flow.CaptchaType.NONE


class FakeManager:
    def __init__(self, state):
        self.auth_machine = SimpleNamespace(state=state, detector=FakeDetector())


def test_unknown_page_on_expected_host_after_challenge_is_not_called_active_captcha():
    page = SimpleNamespace(url="https://hiring.amazon.ca/app#/jobSearch")
    manager = FakeManager(session_refresh.login_flow.AuthState.UNKNOWN_PAGE)

    diag = session_refresh._diagnose_auth_failure(
        page,
        manager,
        session_refresh.login_flow.AuthState.SESSION_ERROR,
        "https://hiring.amazon.ca",
    )

    assert diag["category"] == "post_auth_page_unrecognized"
    assert diag["challenge_type"] == "NONE"
    assert diag["final_host"] == "hiring.amazon.ca"


def test_same_unknown_page_on_wrong_host_stays_state_machine_error():
    page = SimpleNamespace(url="https://example.com/")
    manager = FakeManager(session_refresh.login_flow.AuthState.UNKNOWN_PAGE)

    diag = session_refresh._diagnose_auth_failure(
        page,
        manager,
        session_refresh.login_flow.AuthState.SESSION_ERROR,
        "https://hiring.amazon.ca",
    )

    assert diag["category"] == "state_machine_error"

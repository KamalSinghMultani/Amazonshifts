from __future__ import annotations

from types import SimpleNamespace

import session_proof


class FakePage:
    def __init__(self, url="https://hiring.amazon.ca/app#/jobSearch"):
        self.url = url
        self.visited = []

    def goto(self, url, **_kwargs):
        self.visited.append(url)
        self.url = url

    def wait_for_timeout(self, _ms):
        pass


def _patch_authenticated(monkeypatch, state_name="AUTHENTICATED"):
    state = SimpleNamespace(name=state_name)
    if state_name == "AUTHENTICATED":
        state = session_proof.login_flow.AuthState.AUTHENTICATED

    class Detector:
        def __init__(self, _page):
            pass

        def detect_state(self):
            return state

    monkeypatch.setattr(session_proof.login_flow, "StateDetector", Detector)


def test_fresh_proof_rejects_us_host_for_canadian_config(monkeypatch):
    _patch_authenticated(monkeypatch)
    page = FakePage("https://hiring.amazon.com/app#/jobSearch")

    proof = session_proof.prove_fresh_session(page, "https://hiring.amazon.ca")

    assert proof.passed is False
    assert proof.expected_host == "hiring.amazon.ca"
    assert proof.authenticated_host == "hiring.amazon.com"
    assert "expected hiring.amazon.ca" in proof.reason


def test_fresh_proof_rejects_url_without_positive_auth_state(monkeypatch):
    _patch_authenticated(monkeypatch, "UNKNOWN_PAGE")
    page = FakePage("https://hiring.amazon.ca/app#/jobSearch")

    proof = session_proof.prove_fresh_session(page, "https://hiring.amazon.ca")

    assert proof.passed is False
    assert "positive authenticated UI evidence" in proof.reason


def test_fresh_proof_requires_application_shell_to_stay_on_canada(monkeypatch):
    _patch_authenticated(monkeypatch)
    page = FakePage("https://hiring.amazon.ca/app#/jobSearch")

    def bounced_goto(url, **_kwargs):
        page.visited.append(url)
        page.url = "https://auth.hiring.amazon.com/#/login"

    page.goto = bounced_goto
    monkeypatch.setattr(session_proof.site_selectors, "is_login_page", lambda _page: True)

    proof = session_proof.prove_fresh_session(page, "https://hiring.amazon.ca")

    assert proof.passed is False
    assert proof.application_redirected_to_login is True


def test_fresh_proof_passes_only_with_strong_canadian_auth_and_application_access(monkeypatch):
    _patch_authenticated(monkeypatch)
    page = FakePage("https://hiring.amazon.ca/app#/jobSearch")
    monkeypatch.setattr(session_proof.site_selectors, "is_login_page", lambda _page: False)

    proof = session_proof.prove_fresh_session(page, "https://hiring.amazon.ca")

    assert proof.passed is True
    assert proof.expected_host == "hiring.amazon.ca"
    assert proof.authenticated_host == "hiring.amazon.ca"
    assert proof.authenticated_state == "AUTHENTICATED"
    assert proof.application_host == "hiring.amazon.ca"
    assert page.visited == ["https://hiring.amazon.ca/application/"]


def test_existing_session_first_loads_country_job_search_then_application(monkeypatch):
    _patch_authenticated(monkeypatch)
    page = FakePage("about:blank")
    monkeypatch.setattr(session_proof.site_selectors, "is_login_page", lambda _page: False)

    proof = session_proof.prove_existing_session(page, "https://hiring.amazon.ca")

    assert proof.passed is True
    assert page.visited == [
        "https://hiring.amazon.ca/app#/jobSearch",
        "https://hiring.amazon.ca/application/",
    ]

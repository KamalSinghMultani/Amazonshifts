from __future__ import annotations

from types import SimpleNamespace

import session_proof


class FakeRequest:
    def __init__(self, method="GET"):
        self.method = method


class FakeResponse:
    def __init__(self, url, status, method="GET"):
        self.url = url
        self.status = status
        self.request = FakeRequest(method)


class FakePage:
    def __init__(
        self,
        url="https://hiring.amazon.ca/app#/jobSearch",
        *,
        candidate_status=200,
    ):
        self.url = url
        self.visited = []
        self.waits = []
        self.candidate_status = candidate_status
        self._handlers = {"response": []}

    def on(self, event, callback):
        self._handlers.setdefault(event, []).append(callback)

    def _emit_candidate_response(self):
        if self.candidate_status is None:
            return
        response = FakeResponse(
            "https://hiring.amazon.ca/application/api/candidate-application/candidate",
            self.candidate_status,
        )
        for callback in list(self._handlers.get("response", [])):
            callback(response)

    def goto(self, url, **_kwargs):
        self.visited.append(url)
        self.url = url
        if "hiring.amazon.ca/application/" in url:
            self._emit_candidate_response()

    def wait_for_timeout(self, ms):
        self.waits.append(ms)


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


def test_fresh_proof_reprobes_same_authenticated_application_for_backend_auth(monkeypatch):
    _patch_authenticated(monkeypatch)
    monkeypatch.setattr(session_proof.site_selectors, "is_login_page", lambda _page: False)
    current = "https://hiring.amazon.ca/application/ca/#/consent"
    page = FakePage(current)

    proof = session_proof.prove_fresh_session(page, "https://hiring.amazon.ca")

    assert proof.passed is True
    assert proof.authenticated_state == "AUTHENTICATED"
    assert proof.application_host == "hiring.amazon.ca"
    assert proof.application_backend_authenticated is True
    assert page.visited == [current]
    assert "protected candidate read returned 2xx" in proof.reason


def test_fresh_proof_requires_application_probe_to_stay_on_canada(monkeypatch):
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


def test_fresh_proof_uses_country_specific_consent_probe(monkeypatch):
    _patch_authenticated(monkeypatch)
    page = FakePage("https://hiring.amazon.ca/app#/jobSearch")
    monkeypatch.setattr(session_proof.site_selectors, "is_login_page", lambda _page: False)

    proof = session_proof.prove_fresh_session(page, "https://hiring.amazon.ca")

    assert proof.passed is True
    assert proof.expected_host == "hiring.amazon.ca"
    assert proof.authenticated_host == "hiring.amazon.ca"
    assert proof.authenticated_state == "AUTHENTICATED"
    assert proof.application_host == "hiring.amazon.ca"
    assert proof.application_backend_authenticated is True
    assert page.visited == ["https://hiring.amazon.ca/application/ca/#/consent"]


def test_existing_session_probes_protected_country_route_and_backend(monkeypatch):
    _patch_authenticated(monkeypatch)
    page = FakePage("about:blank")
    monkeypatch.setattr(session_proof.site_selectors, "is_login_page", lambda _page: False)

    proof = session_proof.prove_existing_session(page, "https://hiring.amazon.ca")

    assert proof.passed is True
    assert proof.application_backend_authenticated is True
    assert proof.application_backend_unauthorized is False
    assert page.visited == ["https://hiring.amazon.ca/application/ca/#/consent"]


def test_stale_authenticated_ui_without_candidate_read_cannot_rearm(monkeypatch):
    """Regression for live false RESTORED -> immediate hold login redirect loop."""
    _patch_authenticated(monkeypatch)
    page = FakePage("about:blank", candidate_status=None)
    monkeypatch.setattr(session_proof.site_selectors, "is_login_page", lambda _page: False)

    proof = session_proof.prove_existing_session(page, "https://hiring.amazon.ca")

    assert proof.passed is False
    assert proof.application_redirected_to_login is False
    assert proof.application_backend_authenticated is False
    assert proof.application_backend_unauthorized is False
    assert "no successful protected candidate read" in proof.reason


def test_protected_candidate_401_is_strong_expiry_evidence(monkeypatch):
    _patch_authenticated(monkeypatch)
    page = FakePage("about:blank", candidate_status=401)
    monkeypatch.setattr(session_proof.site_selectors, "is_login_page", lambda _page: False)

    proof = session_proof.prove_existing_session(page, "https://hiring.amazon.ca")

    assert proof.passed is False
    assert proof.application_backend_authenticated is False
    assert proof.application_backend_unauthorized is True
    assert "returned 401" in proof.reason


def test_protected_candidate_403_remains_inconclusive_not_expired(monkeypatch):
    _patch_authenticated(monkeypatch)
    page = FakePage("about:blank", candidate_status=403)
    monkeypatch.setattr(session_proof.site_selectors, "is_login_page", lambda _page: False)

    proof = session_proof.prove_existing_session(page, "https://hiring.amazon.ca")

    assert proof.passed is False
    assert proof.application_backend_authenticated is False
    assert proof.application_backend_unauthorized is False
    assert "no successful protected candidate read" in proof.reason


def test_us_probe_uses_us_application_route():
    assert session_proof._application_probe_url("https://hiring.amazon.com") == (
        "https://hiring.amazon.com/application/us/#/consent"
    )


def test_ca_probe_uses_ca_application_route():
    assert session_proof._application_probe_url("https://hiring.amazon.ca") == (
        "https://hiring.amazon.ca/application/ca/#/consent"
    )

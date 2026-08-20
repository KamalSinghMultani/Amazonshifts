from __future__ import annotations

import inspect
from urllib.parse import parse_qs, urlparse

import relogin
import relogin_patch
import session_proof


def _fragment_query(url: str) -> tuple[str, dict[str, list[str]]]:
    parsed = urlparse(url)
    route, _, query = parsed.fragment.partition("?")
    return route, parse_qs(query)


def test_canadian_auto_login_uses_auth_return_callback():
    url = relogin_patch.auth_entry_url("https://hiring.amazon.ca")
    parsed = urlparse(url)
    route, query = _fragment_query(url)

    assert parsed.hostname == "auth.hiring.amazon.com"
    assert route == "/login"
    assert query["countryCode"] == ["CA"]
    assert query["locale"] == ["en-CA"]
    assert query["redirectUrl"] == ["https://hiring.amazon.ca/app#/auth-return"]
    assert query["destinationUrl"] == [
        "https://hiring.amazon.ca/application/ca/#/consent"
    ]


def test_us_auto_login_uses_us_callback_and_destination():
    url = relogin_patch.auth_entry_url("https://hiring.amazon.com")
    _, query = _fragment_query(url)

    assert query["countryCode"] == ["US"]
    assert query["locale"] == ["en-US"]
    assert query["redirectUrl"] == ["https://hiring.amazon.com/app#/auth-return"]
    assert query["destinationUrl"] == [
        "https://hiring.amazon.com/application/us/#/consent"
    ]


def test_runtime_patch_replaces_bare_login_with_callback_entry():
    relogin_patch.apply_patch(relogin)
    source = inspect.getsource(relogin.AuthenticationStateMachine.run)

    assert "auth_entry_url(base_url)" in source
    assert 'goto("https://auth.hiring.amazon.com/#/login")' not in source


class ReloadingPage:
    def __init__(self):
        self.url = "https://hiring.amazon.ca/application/ca/#/consent"
        self.reloads = 0
        self.gotos: list[str] = []
        self._handlers: dict[str, list] = {"response": []}

    def on(self, event, callback):
        self._handlers.setdefault(event, []).append(callback)

    def _emit_candidate(self, status=200):
        class Request:
            method = "GET"

        class Response:
            url = "https://hiring.amazon.ca/application/api/candidate-application/candidate"
            request = Request()

            def __init__(self, code):
                self.status = code

        for callback in self._handlers.get("response", []):
            callback(Response(status))

    def reload(self, **_kwargs):
        self.reloads += 1
        self._emit_candidate(200)

    def goto(self, url, **_kwargs):
        self.gotos.append(url)
        self.url = url
        self._emit_candidate(200)

    def wait_for_timeout(self, _ms):
        pass


def test_fresh_proof_forces_real_reload_when_auth_lands_on_consent(monkeypatch):
    page = ReloadingPage()

    class Detector:
        def __init__(self, _page):
            pass

        def detect_state(self):
            return session_proof.login_flow.AuthState.AUTHENTICATED

    monkeypatch.setattr(session_proof.login_flow, "StateDetector", Detector)
    monkeypatch.setattr(session_proof.site_selectors, "is_login_page", lambda _page: False)

    proof = session_proof.prove_fresh_session(page, "https://hiring.amazon.ca")

    assert page.reloads == 1
    assert page.gotos == []
    assert proof.passed is True
    assert proof.application_backend_authenticated is True


def test_fresh_proof_still_rejects_backend_401_after_reload(monkeypatch):
    page = ReloadingPage()

    def unauthorized_reload(**_kwargs):
        page.reloads += 1
        page._emit_candidate(401)

    page.reload = unauthorized_reload

    class Detector:
        def __init__(self, _page):
            pass

        def detect_state(self):
            return session_proof.login_flow.AuthState.AUTHENTICATED

    monkeypatch.setattr(session_proof.login_flow, "StateDetector", Detector)
    monkeypatch.setattr(session_proof.site_selectors, "is_login_page", lambda _page: False)

    proof = session_proof.prove_fresh_session(page, "https://hiring.amazon.ca")

    assert proof.passed is False
    assert proof.application_backend_unauthorized is True
    assert "401" in proof.reason

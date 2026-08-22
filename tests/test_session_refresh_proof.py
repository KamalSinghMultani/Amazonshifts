from __future__ import annotations

import inspect
from types import SimpleNamespace

import relogin as login_flow
import session_refresh


def test_refresh_uses_strong_existing_session_proof_not_doctor_shell_check():
    source = inspect.getsource(session_refresh.run)
    assert "prove_existing_session" in source
    assert "doctor.check_portal_login" not in source


def test_definite_expiry_uses_existing_auth_state_machine_for_recovery():
    source = inspect.getsource(session_refresh.run)
    assert "if precheck.passed" in source
    assert "if not _definitive_expiry(precheck)" in source
    assert "_forced_login(page, base_url)" in source
    assert "login_flow.create_auth_system" in inspect.getsource(session_refresh._forced_login)


def test_only_redirect_or_candidate_401_is_definitive_expiry():
    healthy_shape = dict(
        application_redirected_to_login=False,
        application_backend_unauthorized=False,
    )

    assert session_refresh._definitive_expiry(SimpleNamespace(**healthy_shape)) is False
    assert session_refresh._definitive_expiry(SimpleNamespace(
        application_redirected_to_login=True,
        application_backend_unauthorized=False,
    )) is True
    assert session_refresh._definitive_expiry(SimpleNamespace(
        application_redirected_to_login=False,
        application_backend_unauthorized=True,
    )) is True


def test_inconclusive_precheck_returns_without_login(monkeypatch, tmp_path):
    proof = SimpleNamespace(
        passed=False,
        reason="protected candidate read returned 403",
        application_redirected_to_login=False,
        application_backend_unauthorized=False,
        summary=lambda: "backend_authenticated=False; backend_unauthorized=False; passed=False",
        to_dict=lambda: {"passed": False, "application_backend_unauthorized": False},
    )

    class Context:
        pages = [SimpleNamespace()]

        def set_default_timeout(self, _value):
            pass

        def set_default_navigation_timeout(self, _value):
            pass

    class PlaywrightContext:
        def __enter__(self):
            return object()

        def __exit__(self, *_args):
            pass

    config = {
        "browser": {
            "storage_state": str(tmp_path / "saved.json"),
            "action_timeout_ms": 1000,
            "nav_timeout_ms": 1000,
        },
        "site": {"base_url": "https://hiring.amazon.ca"},
    }
    login_calls = []
    result_path = tmp_path / "result.json"

    monkeypatch.setattr(session_refresh, "load_dotenv", lambda: None)
    monkeypatch.setattr(session_refresh, "load_config", lambda _path: config)
    monkeypatch.setattr(session_refresh, "sync_playwright", lambda: PlaywrightContext())
    monkeypatch.setattr(
        session_refresh.browser_launch,
        "launch_context",
        lambda *_args, **_kwargs: (object(), Context()),
    )
    monkeypatch.setattr(session_refresh.browser_launch, "close_context", lambda *_args: None)
    monkeypatch.setattr(session_refresh.session_proof, "prove_existing_session", lambda *_args, **_kwargs: proof)
    monkeypatch.setattr(
        session_refresh,
        "_forced_login",
        lambda *_args: login_calls.append(True) or (_ for _ in ()).throw(AssertionError()),
    )

    rc = session_refresh.run(
        "config.yaml",
        tmp_path / "verified.json",
        result_path,
        force_login=False,
    )
    result = __import__("json").loads(result_path.read_text("utf-8"))

    assert rc == 2
    assert login_calls == []
    assert result["status"] == "inconclusive"
    assert result["definitive_expiry"] is False
    assert "no login attempted" in result["detail"]


def test_successful_login_must_pass_fresh_session_proof_before_persisting():
    source = inspect.getsource(session_refresh.run)
    assert "status == login_flow.OK" in source
    assert "prove_fresh_session" in source
    assert 'status="proof_failed"' in source


def test_forced_login_uses_configured_base_url():
    source = inspect.getsource(session_refresh.run)
    assert "_forced_login(page, base_url)" in source
    assert 'base_url = cfg["site"]["base_url"]' in source


def test_result_file_contains_machine_readable_proof():
    source = inspect.getsource(session_refresh._persist_success)
    assert '"proof": proof.to_dict()' in source
    assert "proof.summary()" in source


def test_refresh_can_prove_an_explicit_newer_input_state():
    source = inspect.getsource(session_refresh.run)
    assert "input_state: Path | None = None" in source
    assert 'storage = input_state or Path(cfg["browser"]["storage_state"])' in source


def test_auth_failure_diagnostics_identify_uncleared_challenge_without_solving(monkeypatch):
    class Detector:
        def detect_captcha_type(self):
            return login_flow.CaptchaType.IMAGE_GRID

    manager = SimpleNamespace(
        auth_machine=SimpleNamespace(
            state=login_flow.AuthState.CAPTCHA_REQUIRED,
            detector=Detector(),
        )
    )
    page = SimpleNamespace(url="https://auth.hiring.amazon.com/#/login")

    result = session_refresh._diagnose_auth_failure(
        page, manager, login_flow.AuthState.SESSION_ERROR
    )

    assert result["category"] == "challenge_not_cleared"
    assert result["machine_state"] == "CAPTCHA_REQUIRED"
    assert result["challenge_type"] == "IMAGE_GRID"
    assert result["final_host"] == "auth.hiring.amazon.com"


def test_late_auth_recheck_accepts_strong_state_after_state_machine_timeout(monkeypatch):
    observed = iter([
        login_flow.AuthState.UNKNOWN_PAGE,
        login_flow.AuthState.AUTHENTICATED,
    ])

    class Detector:
        def detect_state(self):
            return next(observed)

    machine = SimpleNamespace(
        state=login_flow.AuthState.UNKNOWN_PAGE,
        detector=Detector(),
    )
    manager = SimpleNamespace(auth_machine=machine)
    page = SimpleNamespace(
        url="https://hiring.amazon.ca/application/ca/#/consent",
        wait_for_timeout=lambda _ms: None,
    )

    state = session_refresh._late_auth_recheck(
        page,
        manager,
        login_flow.AuthState.SESSION_ERROR,
        "https://hiring.amazon.ca",
        timeout_seconds=1.0,
    )

    assert state == login_flow.AuthState.AUTHENTICATED
    assert machine.state == login_flow.AuthState.AUTHENTICATED


def test_late_auth_recheck_does_not_trust_country_url_alone():
    class Detector:
        def detect_state(self):
            return login_flow.AuthState.UNKNOWN_PAGE

    manager = SimpleNamespace(
        auth_machine=SimpleNamespace(
            state=login_flow.AuthState.UNKNOWN_PAGE,
            detector=Detector(),
        )
    )
    page = SimpleNamespace(
        url="https://hiring.amazon.ca/app#/jobSearch",
        wait_for_timeout=lambda _ms: None,
    )

    state = session_refresh._late_auth_recheck(
        page,
        manager,
        login_flow.AuthState.SESSION_ERROR,
        "https://hiring.amazon.ca",
        timeout_seconds=0,
    )

    assert state == login_flow.AuthState.SESSION_ERROR


def test_diagnostics_do_not_contain_credentials_or_solver_tokens():
    source = inspect.getsource(session_refresh._diagnose_auth_failure)
    assert "AMAZON_LOGIN_EMAIL" not in source
    assert "AMAZON_LOGIN_PIN" not in source
    assert "TWOCAPTCHA_API_KEY" not in source
    assert "context" not in source.lower()
    assert "sitekey" not in source.lower()

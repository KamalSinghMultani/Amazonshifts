from __future__ import annotations

import inspect
from types import SimpleNamespace

import relogin as login_flow
import session_refresh


def test_refresh_uses_strong_existing_session_proof_not_doctor_shell_check():
    source = inspect.getsource(session_refresh.run)
    assert "prove_existing_session" in source
    assert "doctor.check_portal_login" not in source


def test_failed_strong_precheck_uses_existing_auth_state_machine_for_recovery():
    source = inspect.getsource(session_refresh.run)
    assert "if precheck.passed" in source
    assert "_forced_login(" in source
    assert "backend_probe=early_backend_probe" in source
    assert "login_flow.create_auth_system" in inspect.getsource(session_refresh._forced_login)


def test_successful_login_must_pass_fresh_session_proof_before_persisting():
    source = inspect.getsource(session_refresh.run)
    assert "status == login_flow.OK" in source
    assert "prove_fresh_session" in source
    assert 'status="proof_failed"' in source


def test_forced_login_uses_configured_base_url():
    source = inspect.getsource(session_refresh.run)
    assert "_forced_login(" in source
    assert "base_url," in source
    assert 'base_url = cfg["site"]["base_url"]' in source


def test_result_file_contains_machine_readable_proof():
    source = inspect.getsource(session_refresh._persist_success)
    assert '"proof": proof.to_dict()' in source
    assert "proof.summary()" in source


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

from __future__ import annotations

import inspect

import session_refresh


def test_refresh_uses_strong_existing_session_proof_not_doctor_shell_check():
    source = inspect.getsource(session_refresh.run)
    assert "prove_existing_session" in source
    assert "doctor.check_portal_login" not in source


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
    assert "proof=proof.to_dict()" in source
    assert "proof.summary()" in source

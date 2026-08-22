from __future__ import annotations

import inspect

import verify_session


def test_verify_session_defaults_to_reuse_then_recovery():
    source = inspect.getsource(verify_session.main)
    assert '"--force-fresh-login"' in source
    assert "force_login=args.force_fresh_login" in source


def test_verify_session_prints_actionable_auth_failure_fields():
    source = inspect.getsource(verify_session.main)
    for field in (
        "failure category",
        "returned state",
        "machine state",
        "challenge type",
        "final host",
    ):
        assert field in source


def test_verify_session_remains_non_destructive():
    source = inspect.getsource(verify_session)
    assert "Create Application" not in source
    assert "hold_at_application" not in source
    assert "reserve" in (verify_session.__doc__ or "").lower()

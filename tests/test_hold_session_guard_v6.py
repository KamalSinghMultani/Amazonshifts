from __future__ import annotations

import inspect
import time
from pathlib import Path

import real_hold_test
import session_guard_worker
import watcher_v5
import watcher_v6


def test_v6_is_final_layer_over_v5():
    assert issubclass(watcher_v6.HoldReadyWatcher, watcher_v5.PreLiveWatcher)


def test_bat_launches_v6():
    text = Path("run_watcher.bat").read_text("utf-8")
    assert "watcher_v6.py" in text
    assert "watcher_v5.py --config" not in text


def test_health_probe_never_attempts_login():
    source = inspect.getsource(session_guard_worker._prove)
    assert "prove_existing_session" in source
    assert "session_refresh.run" not in source
    assert "_forced_login" not in source


def test_recovery_is_explicit_separate_mode():
    source = inspect.getsource(session_guard_worker._recover)
    assert "session_refresh.run" in source
    assert "force_login=True" in source


def test_proof_worker_marks_only_login_redirect_as_definite_expiry():
    source = inspect.getsource(session_guard_worker._prove)
    assert "application_redirected_to_login" in source
    assert 'status="expired" if definitive else "inconclusive"' in source
    assert "definitive_expiry=definitive" in source


def test_v5_snapshots_live_context_before_proof_or_recovery():
    source = inspect.getsource(watcher_v5.PreLiveWatcher._start_session_worker)
    assert "self.context.storage_state" in source
    assert "session_probe_input_path" in source
    assert 'mode = "recover" if force_login else "prove"' in source
    assert "session_guard_worker.py" in source


def test_v5_blocks_hold_when_session_is_not_verified_and_keeps_schedule_retryable():
    source = inspect.getsource(watcher_v5.PreLiveWatcher._hold)
    assert "_hold_session_ready" in source
    assert "SessionBlockedResult" in source
    assert "schedule remains retryable" in source
    assert watcher_v5.SessionBlockedResult("failed", "x").worth_retrying() is False


def test_v6_stale_proof_fails_closed_and_requests_reproof():
    watcher = object.__new__(watcher_v6.HoldReadyWatcher)
    watcher.session_ok = True
    watcher.session_last_verified_monotonic = time.monotonic() - 1000
    watcher.session_verification_lease_seconds = 600
    watcher.session_check_every = 300
    watcher.next_session_check = 999999.0
    watcher.session_status_reason = ""
    watcher.session_worker = object()  # avoid spawning anything in a unit test
    watcher.holding = True

    assert watcher._hold_session_ready() is False
    assert watcher.session_ok is None
    assert watcher.next_session_check == 0.0


def test_v6_inconclusive_proof_does_not_trigger_login_recovery():
    source = inspect.getsource(watcher_v6.HoldReadyWatcher._poll_session_worker)
    assert "if not definitive_expiry:" in source
    inconclusive = source[source.index("if not definitive_expiry:"):]
    before_definite = inconclusive[:inconclusive.index("# A prove-only worker")]
    assert "force_login=True" not in before_definite
    assert "no login attempted" in before_definite.lower()


def test_v6_definite_expiry_disables_hold_and_starts_recovery():
    source = inspect.getsource(watcher_v6.HoldReadyWatcher._poll_session_worker)
    definite = source[source.index("# A prove-only worker"):]
    assert "_mark_live_session_unhealthy" in definite
    assert "force_login=True" in definite
    assert "definite live-session expiry" in definite


def test_v5_refresh_failure_preserves_current_live_session_truth():
    source = inspect.getsource(watcher_v5.PreLiveWatcher._poll_session_worker)
    forced = source[source.index("if forced:"):]
    assert "Preserve its live hold-ready state" in forced
    assert "self.session_ok = False" not in forced.split("# A prove-only failure", 1)[0]


def test_real_hold_validation_uses_v6_and_preflight_marks_session_ready():
    assert issubclass(real_hold_test.RealHoldTestWatcher, watcher_v6.HoldReadyWatcher)
    source = inspect.getsource(real_hold_test.RealHoldTestWatcher.__init__)
    assert "_mark_session_verified" in source
    assert "real-test preflight" in source

from __future__ import annotations

import watcher_v3
import watcher_v4


def test_live_watcher_starts_strong_session_proof_before_detector_loop(monkeypatch):
    watcher = object.__new__(watcher_v4.AutoSessionWatcher)
    watcher.auto_relogin = True
    watcher.dry_run = False
    watcher.session_check_every = 300
    watcher.next_session_check = 0.0

    calls = []

    def start_worker(self, *, force_login, reason):
        calls.append(("session", force_login, reason))
        return True

    def parent_loop(self, once=False):
        calls.append(("detector", once))

    monkeypatch.setattr(watcher_v4.AutoSessionWatcher, "_start_session_worker", start_worker)
    monkeypatch.setattr(watcher_v3.OptimizedWatcher, "_loop", parent_loop)
    monkeypatch.setattr(watcher_v4.time, "monotonic", lambda: 1000.0)

    watcher_v4.AutoSessionWatcher._loop(watcher, once=False)

    # False means: prove/reuse a valid saved session first. session_refresh is
    # responsible for running the existing auth flow only when that proof fails.
    assert calls[0] == ("session", False, "startup strong session proof/recovery")
    assert calls[1] == ("detector", False)
    assert watcher.next_session_check == 1300.0


def test_once_mode_does_not_leave_a_background_login_worker(monkeypatch):
    watcher = object.__new__(watcher_v4.AutoSessionWatcher)
    watcher.auto_relogin = True
    watcher.dry_run = False

    calls = []

    def start_worker(self, *, force_login, reason):
        calls.append(("session", force_login, reason))
        return True

    def parent_loop(self, once=False):
        calls.append(("detector", once))

    monkeypatch.setattr(watcher_v4.AutoSessionWatcher, "_start_session_worker", start_worker)
    monkeypatch.setattr(watcher_v3.OptimizedWatcher, "_loop", parent_loop)

    watcher_v4.AutoSessionWatcher._loop(watcher, once=True)

    assert calls == [("detector", True)]


def test_dry_run_does_not_perform_login_clicks(monkeypatch):
    watcher = object.__new__(watcher_v4.AutoSessionWatcher)
    watcher.auto_relogin = True
    watcher.dry_run = True

    calls = []

    def start_worker(self, *, force_login, reason):
        calls.append(("session", force_login, reason))
        return True

    def parent_loop(self, once=False):
        calls.append(("detector", once))

    monkeypatch.setattr(watcher_v4.AutoSessionWatcher, "_start_session_worker", start_worker)
    monkeypatch.setattr(watcher_v3.OptimizedWatcher, "_loop", parent_loop)

    watcher_v4.AutoSessionWatcher._loop(watcher, once=False)

    assert calls == [("detector", False)]

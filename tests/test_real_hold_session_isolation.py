from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import real_hold_test


def test_real_hold_validation_reuses_preflight_without_background_auth(monkeypatch):
    cfg = {
        "dry_run": True,
        "hold": {
            "enabled": False,
            "direct_apply": False,
            "stop_before_submit": True,
            "max_per_poll": 1,
        },
        "filters": {},
        "browser": {
            "storage_state": "auth_state.json",
            "user_data_dir": "browser_profile",
            "headless": True,
        },
        "session": {
            "check_every_seconds": 300,
            "relogin_every_seconds": 6000,
            "auto_relogin": True,
        },
        "state": {
            "path": "state/seen_shifts.json",
            "detections_path": "state/detections.jsonl",
        },
    }
    monkeypatch.setattr(real_hold_test, "load_config", lambda _path: cfg)

    out = real_hold_test._prepare_cfg(
        "config.yaml", 60, Path("state/verified.json")
    )

    # The preflight strongly verifies the exact storage state. Keep a harmless
    # prove-only check every five minutes so v6's proof lease cannot go stale
    # during the one-hour mapping run, but disable all actual login/recovery.
    assert out["session"]["check_every_seconds"] == 300
    assert out["session"]["relogin_every_seconds"] == 0
    assert out["session"]["auto_relogin"] is False
    # Compare paths semantically so Windows backslashes and POSIX slashes are
    # treated as the same location.
    assert Path(out["browser"]["storage_state"]) == Path("state/verified.json")


def test_real_hold_preflight_reuses_saved_session_and_requires_proof(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    observed = {}

    def refresh(config_path, output_state, result_path, force_login, *, input_state=None):
        observed.update(
            config_path=config_path,
            output_state=output_state,
            force_login=force_login,
            input_state=input_state,
        )
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(
            json.dumps({"status": "ok", "detail": "protected candidate read returned 2xx"}),
            "utf-8",
        )
        return 0

    monkeypatch.setattr(real_hold_test.session_refresh, "run", refresh)
    ok, verified_state, detail = real_hold_test._preflight("config.yaml")

    assert ok is True
    assert observed["force_login"] is False
    assert observed["input_state"] is None
    assert observed["output_state"] == verified_state
    assert "2xx" in detail


def test_real_hold_preflight_can_explicitly_force_fresh_login(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    observed = {}

    def refresh(config_path, output_state, result_path, force_login, *, input_state=None):
        observed["force_login"] = force_login
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(
            json.dumps({"status": "ok", "detail": "protected candidate read returned 2xx"}),
            "utf-8",
        )
        return 0

    monkeypatch.setattr(real_hold_test.session_refresh, "run", refresh)
    ok, _verified_state, _detail = real_hold_test._preflight(
        "config.yaml", force_fresh_login=True
    )

    assert ok is True
    assert observed["force_login"] is True


def test_real_hold_preflight_reuses_its_last_strictly_verified_state(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    prior = tmp_path / "state" / "real_test_verified_state.json"
    prior.parent.mkdir(parents=True)
    prior.write_text("{}", "utf-8")
    observed = {}

    def refresh(config_path, output_state, result_path, force_login, *, input_state=None):
        observed["input_state"] = input_state
        result_path.write_text(
            json.dumps({"status": "healthy", "detail": "protected candidate read returned 2xx"}),
            "utf-8",
        )
        return 0

    monkeypatch.setattr(real_hold_test.session_refresh, "run", refresh)
    monkeypatch.setattr(
        real_hold_test,
        "load_config",
        lambda _path: {"browser": {"storage_state": str(tmp_path / "auth_state.json")}},
    )

    ok, verified_state, _detail = real_hold_test._preflight("config.yaml")

    assert ok is True
    assert observed["input_state"] == verified_state == Path(
        "state/real_test_verified_state.json"
    )


def test_strictly_verified_state_is_promoted_to_canonical_restart_seed(monkeypatch, tmp_path):
    verified = tmp_path / "state" / "verified.json"
    canonical = tmp_path / "auth_state.json"
    verified.parent.mkdir(parents=True)
    verified.write_text('{"cookies": [], "origins": []}', "utf-8")
    monkeypatch.setattr(
        real_hold_test,
        "load_config",
        lambda _path: {"browser": {"storage_state": str(canonical)}},
    )

    real_hold_test._promote_verified_state("config.yaml", verified)

    assert canonical.read_text("utf-8") == verified.read_text("utf-8")


def test_sixty_minute_timer_starts_only_after_strict_preflight(monkeypatch, tmp_path):
    events = []
    cfg = {
        "session": {"auto_relogin": False, "relogin_every_seconds": 0},
        "browser": {},
        "hold": {},
        "state": {},
    }

    monkeypatch.setattr(real_hold_test, "load_dotenv", lambda: None)
    monkeypatch.setattr(real_hold_test, "load_config", lambda _path: {})
    monkeypatch.setattr(real_hold_test, "setup_logging", lambda _cfg: None)
    monkeypatch.setattr(
        real_hold_test,
        "_preflight",
        lambda _path, **_kwargs: events.append("strict-proof") or (True, tmp_path / "verified.json", "2xx"),
    )
    monkeypatch.setattr(real_hold_test, "_prepare_cfg", lambda *_args: cfg)
    monkeypatch.setattr(real_hold_test, "_reset_test_state", lambda _cfg: None)
    monkeypatch.setattr(
        real_hold_test, "_write_runtime_config", lambda _cfg: tmp_path / "runtime.yaml"
    )
    monkeypatch.setattr(real_hold_test.hold_metrics, "count", lambda: 0)
    monkeypatch.setattr(real_hold_test.hold_metrics, "latest_after", lambda _n: None)

    class Watcher:
        def __init__(self, supplied, live_override):
            assert supplied["session"]["auto_relogin"] is False
            assert supplied["session"]["relogin_every_seconds"] == 0
            assert live_override is True
            self.stop_event = SimpleNamespace(set=lambda: None)
            self.config_path = ""

        def run(self, once):
            events.append("watcher-run")
            assert once is False
            return 0

    class Timer:
        def __init__(self, seconds, callback):
            assert seconds == 60 * 60
            assert callable(callback)
            self.daemon = False

        def start(self):
            events.append("timer-start")

        def cancel(self):
            events.append("timer-cancel")

    monkeypatch.setattr(real_hold_test, "RealHoldTestWatcher", Watcher)
    monkeypatch.setattr(real_hold_test.threading, "Timer", Timer)

    assert real_hold_test.main(["--ack-real-hold", "--minutes", "60"]) == 0
    assert events[:3] == ["strict-proof", "timer-start", "watcher-run"]

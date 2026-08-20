from __future__ import annotations

from pathlib import Path

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

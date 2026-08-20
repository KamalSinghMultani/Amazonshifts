"""Explicit, time-bounded real Canada hold validation.

This command is intentionally separate from run_watcher.bat.  It mutates only
an in-memory config copy, never config.yaml.  It broadens matching to whatever
the already-Canadian API returns, runs for at most N minutes (max 60), and stops
immediately after the first confirmed or uncertain Create Application attempt.

A confirmed run creates a real Amazon candidate application / soft reserve.
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import threading
from datetime import datetime, timedelta
from pathlib import Path

import yaml

import hold_metrics
import session_refresh
import site_selectors
import watcher_v5
from config import load_config, load_dotenv, setup_logging


log = logging.getLogger("watcher")


class RealHoldTestWatcher(watcher_v5.PreLiveWatcher):
    def _hold(self, shift, poll_started=None):
        result = super()._hold(shift, poll_started=poll_started)
        if result is not None and (
            result.held or result.status == site_selectors.UNCERTAIN
        ):
            # Never create a second application during a validation run.  An
            # uncertain result means Create Application may already have been
            # accepted, so it is treated as stop-now just like confirmation.
            log.info("real hold validation stopping after first committed/uncertain attempt")
            self.stop_event.set()
        return result


def _preflight(config_path: str) -> tuple[bool, Path, str]:
    state_dir = Path("state")
    verified_state = state_dir / "real_test_verified_state.json"
    result_path = state_dir / "real_test_session_result.json"
    rc = session_refresh.run(
        config_path,
        output_state=verified_state,
        result_path=result_path,
        force_login=False,
    )
    try:
        result = json.loads(result_path.read_text("utf-8"))
    except Exception as exc:  # noqa: BLE001
        return False, verified_state, f"could not read session preflight result: {exc}"
    if rc == 0 and result.get("status") in ("ok", "healthy"):
        return True, verified_state, str(result.get("detail") or "session verified")
    detail = str(result.get("detail") or f"session preflight exited {rc}")
    return False, verified_state, detail


def _prepare_cfg(config_path: str, minutes: int, verified_state: Path) -> dict:
    cfg = copy.deepcopy(load_config(config_path))
    end = datetime.now() + timedelta(minutes=minutes)

    # Explicit live validation, one reservation at most per poll and one total
    # because RealHoldTestWatcher stops after the first committed/uncertain one.
    cfg["dry_run"] = False
    cfg["hold"]["enabled"] = True
    cfg["hold"]["direct_apply"] = True
    cfg["hold"]["stop_before_submit"] = False
    cfg["hold"]["max_per_poll"] = 1

    # The API request itself is already scoped to Canada.  This bypasses only
    # the user's normal title/location preferences, and expires automatically.
    cfg.setdefault("filters", {})["accept_everything_until"] = end.isoformat(timespec="seconds")

    # Use exactly the isolated storage state that just passed preflight.  A
    # Playwright persistent profile ignores storage_state entirely, so leaving
    # user_data_dir enabled here would prove one browser and test another.
    # This mutation exists only in memory for this validation process.
    cfg["browser"]["storage_state"] = str(verified_state)
    cfg["browser"]["user_data_dir"] = None

    # Validation must neither skip candidates already seen by the normal
    # watcher nor contaminate its dedup/history files.  Give it an isolated
    # state namespace that disappears into the normal gitignored state/ tree.
    cfg.setdefault("state", {})["path"] = "state/real_hold_test_seen.json"
    cfg["state"]["detections_path"] = "state/real_hold_test_detections.jsonl"
    return cfg


def _write_runtime_config(cfg: dict) -> Path:
    """Write the in-memory test config for background session workers.

    v3 session workers receive a config path, not the parent's in-memory dict.
    Pointing them at normal config.yaml would make a worker reopen the old
    auth_state.json immediately after preflight had proved/recovered a newer
    real_test_verified_state.json.  This gitignored runtime config keeps the
    main watcher and every helper on the exact same proved session source.
    """
    runtime = copy.deepcopy(cfg)
    # validate_config recreates this derived value when the worker reloads.
    (runtime.get("polling") or {}).pop("hot_windows_parsed", None)
    path = Path("state/real_hold_test_runtime.yaml")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(runtime, sort_keys=False), "utf-8")
    return path


def _reset_test_state(cfg: dict) -> None:
    for key in ("path", "detections_path"):
        value = (cfg.get("state") or {}).get(key)
        if not value:
            continue
        try:
            Path(value).unlink(missing_ok=True)
        except OSError as exc:
            log.warning("could not reset real-test state %s: %s", value, exc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run one explicitly acknowledged, time-bounded real Canada hold test."
    )
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--minutes", type=int, default=60)
    parser.add_argument(
        "--ack-real-hold",
        action="store_true",
        help="required: acknowledge that Create Application can reserve a real shift",
    )
    args = parser.parse_args(argv)

    if not args.ack_real_hold:
        print("NOT STARTED — this command can create a real Amazon application/soft reserve.")
        print("Re-run with --ack-real-hold only when you intentionally want the real test.")
        return 2
    if args.minutes < 1 or args.minutes > 60:
        print("NOT STARTED — --minutes must be between 1 and 60.")
        return 2

    load_dotenv()
    cfg_for_logging = load_config(args.config)
    setup_logging(cfg_for_logging)

    print("Running strong Canada application-session preflight...")
    ok, verified_state, detail = _preflight(args.config)
    if not ok:
        print(f"NOT STARTED — session preflight failed: {detail}")
        print("Check screenshots/ for a session failure image/JSON sidecar if one was captured.")
        return 3

    cfg = _prepare_cfg(args.config, args.minutes, verified_state)
    _reset_test_state(cfg)
    runtime_config = _write_runtime_config(cfg)
    metrics_before = hold_metrics.count()
    deadline = datetime.now() + timedelta(minutes=args.minutes)
    print("SESSION READY — real hold validation is armed.")
    print(f"Canada-wide matching ends automatically at {deadline:%Y-%m-%d %H:%M:%S} local time.")
    print("The process also stops immediately after the first confirmed or uncertain Create Application attempt.")
    print("Timing records: logs/hold_timings.jsonl")

    watcher = RealHoldTestWatcher(cfg, live_override=True)
    # Make v4/v3 background health/re-login workers use the same isolated,
    # preflight-verified test state instead of normal config.yaml.
    watcher.config_path = str(runtime_config)
    timer = threading.Timer(args.minutes * 60, watcher.stop_event.set)
    timer.daemon = True
    timer.start()
    try:
        rc = watcher.run(once=False)
    finally:
        timer.cancel()

    latest = hold_metrics.latest_after(metrics_before)
    if latest:
        print("THIS RUN HOLD TIMING")
        print(f"  status:             {latest.get('status')}")
        print(f"  poll -> dispatch:   {latest.get('poll_to_dispatch_ms')} ms")
        print(f"  poll -> final:      {latest.get('total_from_poll_ms')} ms")
        stages = latest.get("stages") or []
        for stage in stages:
            print(f"  {stage.get('name')}: {stage.get('ms_from_hold_start')} ms from hold start")
    else:
        print("No hold attempt occurred during this validation window.")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())

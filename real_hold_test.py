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

    # Use the state that just passed the strong application-session proof for
    # this process.  Do not overwrite the user's normal config file.
    cfg["browser"]["storage_state"] = str(verified_state)
    return cfg


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
    setup_logging(cfg_for_logging.get("logging") or {})

    print("Running strong Canada application-session preflight...")
    ok, verified_state, detail = _preflight(args.config)
    if not ok:
        print(f"NOT STARTED — session preflight failed: {detail}")
        print("Check screenshots/ for a session failure image/JSON sidecar if one was captured.")
        return 3

    cfg = _prepare_cfg(args.config, args.minutes, verified_state)
    deadline = datetime.now() + timedelta(minutes=args.minutes)
    print("SESSION READY — real hold validation is armed.")
    print(f"Canada-wide matching ends automatically at {deadline:%Y-%m-%d %H:%M:%S} local time.")
    print("The process also stops immediately after the first confirmed or uncertain Create Application attempt.")
    print("Timing records: logs/hold_timings.jsonl")

    watcher = RealHoldTestWatcher(cfg, live_override=True)
    timer = threading.Timer(args.minutes * 60, watcher.stop_event.set)
    timer.daemon = True
    timer.start()
    try:
        rc = watcher.run(once=False)
    finally:
        timer.cancel()

    latest = hold_metrics.latest()
    if latest:
        print("LATEST HOLD TIMING")
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

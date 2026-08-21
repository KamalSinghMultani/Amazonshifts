"""Explicit, time-bounded real Canada reservation validation.

This command is intentionally separate from run_watcher.bat. It mutates only
an in-memory config copy, never config.yaml. It broadens matching to whatever
the already-Canadian API returns, runs for at most N minutes (max 60), and stops
when a reserve is confirmed or the post-commit state is genuinely uncertain.

For this validation only, the browser-driven fast path may click the observed
Application Integrity Notice's I Agree control after Create Application, then
passively wait for either a verified soft reserve or an unavailable result. A
narrow, explicit unavailable result is retryable: the watcher may move to the
next ranked schedule within its normal attempt budget. It does not fill personal
information, documents, assessments, identity checks, or later application
fields. A confirmed run creates a real Amazon candidate application / soft
reserve.
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
import safe_research_trace
import session_refresh
import site_selectors
import watcher_v6
from config import load_config, load_dotenv, setup_logging


log = logging.getLogger("watcher")


class RealHoldTestWatcher(watcher_v6.HoldReadyWatcher):
    def __init__(self, cfg: dict, live_override: bool = False) -> None:
        super().__init__(cfg, live_override=live_override)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.research_trace_path = Path(f"logs/real_hold_research-{stamp}.jsonl")
        self.research_trace: safe_research_trace.SafeResearchTrace | None = None
        # main() only constructs this watcher after _preflight() strongly proved
        # the exact storage state assigned to cfg.browser.storage_state. The
        # normal watcher begins unverified and proves itself in the background;
        # this isolated <=60-minute test can safely start armed immediately.
        self._mark_session_verified("real-test preflight strongly proved this exact state", notify=False)

    def _start_api_mode(self, browser_cfg: dict) -> None:
        # Attach before the job-search and application pages are created.  The
        # trace is passive and sanitized; it never reads auth headers, cookies,
        # storage, application/auth/KYC bodies, or sensitive login/KYC data.
        # Only known-public job catalog response JSON is retained for research.
        self.research_trace = safe_research_trace.SafeResearchTrace(
            self.context, self.research_trace_path
        ).start()
        try:
            return super()._start_api_mode(browser_cfg)
        except Exception:
            self.research_trace.stop()
            raise

    def _hold(self, shift, poll_started=None):
        result = super()._hold(shift, poll_started=poll_started)
        integrity_attempted = bool(
            result is not None
            and any(
                name == "integrity agree clicked"
                for name, _ms in (getattr(result, "timings", ()) or ())
            )
        )

        if result is not None and (
            result.held
            or result.status in (
                site_selectors.UNCERTAIN,
                site_selectors.IDENTITY_VERIFICATION_REQUIRED,
            )
        ):
            # Confirmed means we won. UNCERTAIN means a committed application
            # may exist, so trying another schedule could create ambiguity.
            log.info("real hold validation stopping after confirmed/uncertain reservation attempt")
            self.stop_event.set()
        elif (
            result is not None
            and integrity_attempted
            and result.status == site_selectors.FAILED
        ):
            # The fast path only returns this post-I-Agree FAILED state when it
            # has narrow visible evidence that the schedule is unavailable.
            # No reserve was confirmed, so let v3's normal retry loop advance
            # immediately to the next ranked candidate within the attempt budget.
            log.info("schedule explicitly unavailable after I Agree; trying next ranked schedule if available")
        return result

    def run(self, once: bool = False) -> int:
        try:
            return super().run(once=once)
        finally:
            if self.research_trace is not None:
                self.research_trace.stop()


def _preflight(config_path: str) -> tuple[bool, Path, str]:
    state_dir = Path("state")
    verified_state = state_dir / "real_test_verified_state.json"
    result_path = state_dir / "real_test_session_result.json"
    rc = session_refresh.run(
        config_path,
        output_state=verified_state,
        result_path=result_path,
        force_login=True,
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

    # One reserve maximum, but up to three ranked candidates may be attempted
    # when earlier ones return the explicit post-I-Agree unavailable state.
    cfg["dry_run"] = False
    cfg["hold"]["enabled"] = True
    cfg["hold"]["direct_apply"] = True
    cfg["hold"]["prewarm_application"] = True
    cfg["hold"]["compatibility_fallback"] = False
    cfg["hold"]["stop_before_submit"] = False
    cfg["hold"]["max_per_poll"] = 1
    cfg["hold"]["job_attempts"] = max(3, int(cfg["hold"].get("job_attempts", 3)))

    # Reservation-only integrity mode. The fast path clicks only I Agree, then
    # waits for JOB_SELECTED + exact schedule + soft reserve expiry, or a narrow
    # visible unavailable result. It never continues into later form fields.
    cfg["hold"]["manual_integrity_wait"] = False
    cfg["hold"]["manual_integrity_timeout_ms"] = 120000
    cfg["hold"]["auto_integrity_agree"] = True
    # The account has already completed eKYC. Click only Amazon Hiring's
    # launcher once so Amazon can recognize that fact and skip the gate. The
    # hold path still stops if a real remoteKYC/selfie/document UI appears.
    cfg["hold"]["auto_start_identity_verification"] = True

    # The API request itself is already scoped to Canada. This bypasses only
    # the user's normal title/location preferences, and expires automatically.
    cfg.setdefault("filters", {})["accept_everything_until"] = end.isoformat(timespec="seconds")

    # Use exactly the isolated storage state that just passed preflight. A
    # Playwright persistent profile ignores storage_state entirely, so leaving
    # user_data_dir enabled here would prove one browser and test another.
    cfg["browser"]["storage_state"] = str(verified_state)
    cfg["browser"]["user_data_dir"] = None

    # Keep the first automated integrity validation visible so the exact page
    # after I Agree can be inspected if Amazon returns an unexpected state.
    # Normal watcher/headless configuration is untouched.
    cfg["browser"]["headless"] = False

    # The preflight immediately above already performed a strong protected
    # application-session proof. Keep only the harmless prove-only health check
    # during this <=60-minute run so the v6 proof lease is renewed before it can
    # become stale. Do NOT run proactive or expiry-triggered login/recovery in
    # this mapping test: health proof never enters credentials or solves a
    # challenge, while the normal long-running watcher keeps full recovery.
    session_cfg = cfg.setdefault("session", {})
    session_cfg["check_every_seconds"] = 300
    session_cfg["relogin_every_seconds"] = 0
    session_cfg["auto_relogin"] = False

    # Validation must neither skip candidates already seen by the normal
    # watcher nor contaminate its dedup/history files. Give it an isolated
    # state namespace that disappears into the normal gitignored state/ tree.
    cfg.setdefault("state", {})["path"] = "state/real_hold_test_seen.json"
    cfg["state"]["detections_path"] = "state/real_hold_test_detections.jsonl"
    return cfg


def _write_runtime_config(cfg: dict) -> Path:
    """Write the in-memory test config for any diagnostic helper."""
    runtime = copy.deepcopy(cfg)
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
        description="Run one explicitly acknowledged, time-bounded real Canada reservation test."
    )
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--minutes", type=int, default=60)
    parser.add_argument(
        "--ack-real-hold",
        action="store_true",
        help="required: acknowledge that the test can create an application and reserve a real shift",
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

    print("Running fresh Canada login and strict application-session proof...")
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
    print("SESSION READY — reservation-only validation is armed.")
    print(f"Canada-wide matching ends automatically at {deadline:%Y-%m-%d %H:%M:%S} local time.")
    print("A visible Chrome window will stay open for this first integrity transition test.")
    print("The freshly proved session is reused directly; prove-only health checks renew hold readiness every 5 minutes, with no mid-test login/recovery.")
    print("Flow: detect -> exact schedule -> Create Application -> I Agree -> reserve result.")
    print("Confirmed or uncertain -> STOP. Explicit unavailable -> try next ranked schedule (up to 3+ configured attempts).")
    print("Later personal-info/documents/assessment/identity steps are never filled or clicked.")
    print("DOM timing markers will show document start, DOMContentLoaded, and when Create first enters/enables in the DOM.")
    print("Timing records: logs/hold_timings.jsonl")

    watcher = RealHoldTestWatcher(cfg, live_override=True)
    research_trace_path = getattr(watcher, "research_trace_path", None)
    if research_trace_path:
        print(f"Sanitized local browser/network research trace: {research_trace_path}")
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
        if latest.get("backend_detail"):
            print(f"  backend:            {latest.get('backend_detail')}")
    else:
        print("No hold attempt occurred during this validation window.")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())

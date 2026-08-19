"""Second optimization pass for the schedule-aware watcher.

Adds the remaining race/reliability fixes without replacing Amazon's own
Create Application UI flow:

* batch schedule lookups to avoid N+1 GraphQL latency during large drops;
* run session health/re-login in a separate process so 2-second detection keeps
  running while OTP/challenge work is happening;
* count only FAILED refreshes against the daily safety budget;
* verify the soft reserve from update-application responses when available;
* fall back to the proven click path when the direct application route fails.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import hold_verify
import schedule_batch
import schedules as schedules_mod
import site_selectors
import watcher as base
import watcher_v2
from shift_matcher import Shift

log = logging.getLogger("watcher")


def _config_path_from_argv() -> str:
    argv = sys.argv[1:]
    for i, arg in enumerate(argv):
        if arg == "--config" and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith("--config="):
            return arg.split("=", 1)[1]
    return "config.yaml"


class OptimizedWatcher(watcher_v2.ScheduleAwareWatcher):
    def __init__(self, cfg: dict, live_override: bool = False) -> None:
        super().__init__(cfg, live_override=live_override)
        self.config_path = _config_path_from_argv()

        state_dir = Path(cfg["state"]["path"]).parent
        self.refresh_state_path = state_dir / "session_refresh_state.json"
        self.refresh_result_path = state_dir / "session_refresh_result.json"
        self.session_worker: subprocess.Popen | None = None
        self.session_worker_reason = ""

        # The old counter capped every successful 100-minute refresh too, which
        # exhausted a 12/day budget before 24 hours elapsed. Only failures are
        # dangerous; successful proactive refreshes are not counted here.
        self.failed_relogins_today = 0
        self.failed_relogin_day = datetime.now().date()
        self.max_failed_relogins_per_day = int(
            (cfg.get("session") or {}).get("max_relogins_per_day") or 0
        )

        # Start health checks on the configured cadence rather than immediately
        # after startup. The freshly opened browser/token page is already proof
        # enough to let the first detection poll run without extra navigation.
        now = time.monotonic()
        self.next_session_check = now + self.session_check_every if self.session_check_every else 0
        self.next_relogin = now + self.relogin_every if self.relogin_every else 0

    # ── non-blocking session maintenance ───────────────────────────────────
    def _roll_failed_relogin_day(self) -> None:
        today = datetime.now().date()
        if today != self.failed_relogin_day:
            self.failed_relogin_day = today
            self.failed_relogins_today = 0
            self.relogin_blocked = False

    def _failure_budget_left(self) -> bool:
        self._roll_failed_relogin_day()
        if not self.max_failed_relogins_per_day:
            return True
        return self.failed_relogins_today < self.max_failed_relogins_per_day

    def _start_session_worker(self, *, force_login: bool, reason: str) -> bool:
        if self.session_worker is not None:
            return False
        if self.holding:
            return False
        self._roll_failed_relogin_day()
        if self.relogin_blocked or not self._failure_budget_left():
            return False

        self.refresh_state_path.parent.mkdir(parents=True, exist_ok=True)
        for path in (self.refresh_state_path, self.refresh_result_path):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

        cmd = [
            sys.executable,
            str(Path(__file__).with_name("session_refresh.py")),
            "--config", self.config_path,
            "--output-state", str(self.refresh_state_path),
            "--result", str(self.refresh_result_path),
        ]
        if force_login:
            cmd.append("--force-login")

        try:
            self.session_worker = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=str(Path(__file__).resolve().parent),
            )
            self.session_worker_reason = reason
            log.info("session maintenance started in background (%s)", reason)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("could not start background session maintenance: %s", exc)
            return False

    def _apply_refreshed_state(self) -> None:
        payload = json.loads(self.refresh_state_path.read_text("utf-8"))
        cookies = payload.get("cookies") if isinstance(payload, dict) else None
        if cookies:
            self.context.add_cookies(cookies)

        # Re-mint the live GraphQL token from the newly refreshed cookies. The
        # detector page itself is kept; no shift search state is lost.
        if self.page is not None:
            try:
                self.page.reload(wait_until="domcontentloaded")
                self.page.wait_for_timeout(500)
                if self.token_source is not None:
                    self.token_source.refresh()
            except Exception as exc:  # noqa: BLE001
                log.warning("session cookies imported but token-page reload failed: %s", exc)

        # Persist the merged live context so a restart inherits the refresh.
        try:
            self.context.storage_state(path=self.cfg["browser"]["storage_state"])
        except Exception as exc:  # noqa: BLE001
            log.debug("could not persist imported session state: %s", exc)

    def _poll_session_worker(self) -> None:
        worker = self.session_worker
        if worker is None or worker.poll() is None:
            return

        self.session_worker = None
        status = "error"
        detail = f"session worker exited {worker.returncode}"
        try:
            result = json.loads(self.refresh_result_path.read_text("utf-8"))
            status = str(result.get("status") or status)
            detail = str(result.get("detail") or detail)
        except Exception as exc:  # noqa: BLE001
            detail = f"could not read session worker result: {exc}"

        if status in ("ok", "healthy"):
            try:
                if self.refresh_state_path.exists():
                    self._apply_refreshed_state()
                self.session_ok = True
                self.relogin_tried = False
                log.info("background session maintenance succeeded (%s)", detail)
                return
            except Exception as exc:  # noqa: BLE001
                status = "error"
                detail = f"refresh succeeded but importing state failed: {exc}"

        self.failed_relogins_today += 1
        self.session_ok = False
        log.warning(
            "background session maintenance failed (%s): %s [failed today %d/%s]",
            status, detail, self.failed_relogins_today,
            self.max_failed_relogins_per_day or "unlimited",
        )

        if status == "captcha":
            self.relogin_blocked = True
            log.error("CAPTCHA blocked background login; automatic refresh paused")

        if not self._failure_budget_left():
            self.relogin_blocked = True
            log.error("failed-login budget exhausted; automatic refresh paused until tomorrow")

        if self.alert_on_expiry:
            self.notify_async(
                self.notifier.notify_error,
                "Background Amazon session refresh failed: "
                f"{detail}. Detection is still running; holding may need a manual login.",
            )

    def session_maintenance_tick(self) -> None:
        """Advance maintenance without ever waiting for it in the detector loop."""
        self._poll_session_worker()
        if self.session_worker is not None or self.holding or self.dry_run:
            return

        now = time.monotonic()
        # Proactive replacement wins over a health-only check when both are due.
        if self.auto_relogin and self.relogin_every and now >= self.next_relogin:
            self.next_relogin = now + self.relogin_every
            self._start_session_worker(force_login=True, reason="proactive refresh")
            return

        if self.session_check_every and now >= self.next_session_check:
            self.next_session_check = now + self.session_check_every
            self._start_session_worker(force_login=False, reason="health check")

    def _loop(self, once: bool = False) -> None:
        polling = self.cfg["polling"]
        while not self.stop_event.is_set():
            try:
                self.poll_once()
                self.consecutive_errors = 0
            except Exception as exc:  # noqa: BLE001
                self.consecutive_errors += 1
                log.exception("poll failed (%d in a row)", self.consecutive_errors)
                if self.consecutive_errors >= polling["max_consecutive_errors"]:
                    self._trip_circuit_breaker(exc)

            # Unlike the old loop this call never performs page/OTP work in the
            # detector process. A child process does that while polling carries on.
            self.session_maintenance_tick()

            if once or self.stop_event.is_set():
                break
            delay, hot = self._next_delay()
            self.stop_event.wait(delay)

    # ── batch schedule discovery ────────────────────────────────────────────
    def _batch_expand(self, jobs: list[Shift]) -> list[Shift] | None:
        if self.mode != "api" or self.api_client is None:
            return None
        ids = [job.id for job in jobs if job.id]
        if not ids:
            return []
        try:
            by_job = schedule_batch.fetch(self.api_client, ids, chunk_size=20)
        except Exception as exc:  # noqa: BLE001
            log.warning("batched schedule lookup failed; using per-job fallback: %s", exc)
            return None

        out: list[Shift] = []
        for job in jobs:
            if not job.id:
                continue
            for schedule in schedules_mod.bookable(by_job.get(job.id, [])):
                ok, reason = self._schedule_is_acceptable(schedule)
                if not ok:
                    log.debug("skip schedule %s (%s)", schedule.id, reason)
                    continue
                out.append(self._schedule_shift(job, schedule))
        return out

    def poll_once(self) -> None:  # noqa: C901 - latency-critical path kept explicit
        self.polls += 1
        started = time.perf_counter()
        hot = self.is_hot()
        jobs = self._fetch_shifts()
        fetch_ms = (time.perf_counter() - started) * 1000
        log.info(
            "poll %d: %d job card(s) in %.0fms%s",
            self.polls, len(jobs), fetch_ms, " [hot]" if hot else "",
        )

        rejected: list[str] = []
        matched_jobs: list[Shift] = []
        for job in jobs:
            matched, reason = self.matcher.matches(job)
            if matched:
                matched_jobs.append(job)
            else:
                rejected.append(f"{job.summary()} [{reason}]")

        if rejected:
            log.info(
                "%d posting(s) seen but filtered out: %s",
                len(rejected), " | ".join(rejected[:5]),
            )

        expanded = self._batch_expand(matched_jobs)
        candidates: list[Shift] = []
        if expanded is None:
            # Preserve v2 correctness if Amazon rejects an aliased GraphQL
            # document: use the proven per-job schedule requests for this poll.
            for job in matched_jobs:
                per_job = self._expand_job_to_schedules(job)
                if per_job is None:
                    done_key, _ = self._candidate_key(job)
                    if not self.state.has_seen(done_key):
                        candidates.append(job)
                else:
                    candidates.extend(per_job)
        else:
            candidates.extend(expanded)

        candidates = [
            shift for shift in candidates
            if not self.state.has_seen(self._candidate_key(shift)[0])
        ]
        if not candidates:
            return

        self.go_hot()
        candidates = self.ranker.sort(candidates)
        holding = (
            not self.dry_run
            and self.cfg["hold"]["enabled"]
            and site_selectors.detection_ready()
        )
        alert_cap = self.cfg["notifications"].get("max_alerts_per_poll") or len(candidates)

        newly_alerted: list[Shift] = []
        for shift in candidates:
            done_key, alert_key = self._candidate_key(shift)
            if self.state.has_seen(alert_key):
                continue
            self.state.mark_seen(alert_key, shift.summary())
            self.state.log_detection(done_key, shift.summary())
            self.alerts += 1
            newly_alerted.append(shift)
            log.info("MATCH: %s [%s]", shift.summary(), self.ranker.explain(shift))
        self.state.save()

        if not holding:
            for shift in newly_alerted[:alert_cap]:
                self.notifier.notify_shift(shift, dry_run=self.dry_run)
                done_key, _ = self._candidate_key(shift)
                self.state.mark_seen(done_key, shift.summary())
            self.state.save()
            self._send_digest(max(0, len(newly_alerted) - alert_cap))
            return

        hold_cap = max(1, int(self.cfg["hold"].get("max_per_poll", 1)))
        job_attempts = max(1, int(self.cfg["hold"].get("job_attempts", 3)))
        budget = float(self.cfg["hold"].get("attempt_budget_seconds", 45))
        to_try = candidates[:max(job_attempts, hold_cap)]

        newly_alerted_ids = {s.id for s in newly_alerted}
        for shift in to_try[:hold_cap]:
            if shift.id in newly_alerted_ids:
                self.notify_async(self.notifier.notify_shift, shift, dry_run=False)

        log.info(
            "holding %s — %.0fms after poll start",
            to_try[0].summary(), (time.perf_counter() - started) * 1000,
        )
        self.holding = True
        held = 0
        tried = 0
        try:
            for position, shift in enumerate(to_try):
                if held >= hold_cap:
                    break
                if position and (time.perf_counter() - started) > budget:
                    log.info("hold budget exhausted after %d attempt(s)", tried)
                    break

                outcome = self._hold(shift, poll_started=started)
                tried += 1
                done_key, _ = self._candidate_key(shift)
                if outcome is None or outcome.held or outcome.status == site_selectors.UNCERTAIN:
                    self.state.mark_seen(done_key, shift.summary())
                    held += 1
                    continue

                log.info("reservation failed; schedule remains retryable next poll: %s", shift.id)
                if not outcome.worth_retrying():
                    if "login" in (outcome.message or "").lower():
                        self._start_session_worker(force_login=True, reason="hold reported dead session")
                    break
        finally:
            self.holding = False
            self.state.save()

        attempted_ids = {s.id for s in to_try[:hold_cap]}
        for shift in newly_alerted[:alert_cap]:
            if shift.id not in attempted_ids:
                self.notify_async(self.notifier.notify_shift, shift, dry_run=True)
        self._send_digest(max(0, len(newly_alerted) - alert_cap))

    # ── direct route + backend verification + fallback ─────────────────────
    def _direct_hold(self, shift: Shift, poll_started: float | None = None):
        raw = shift.raw or {}
        schedule_id = raw.get("scheduleId")
        job_id = raw.get("jobId") or raw.get("parentJobId")
        if not (schedule_id and job_id and self.cfg["hold"].get("direct_apply", True)):
            return None

        page = self.page
        if page is None:
            page = self.context.new_page()
            self.page = page

        base.SCREENSHOT_DIR.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        shot = base.SCREENSHOT_DIR / f"hold-{stamp}.png"
        direct = schedules_mod.application_url(
            self.cfg["site"]["base_url"], str(job_id), str(schedule_id)
        )

        log.info("holding schedule %s via direct application URL", schedule_id)
        with hold_verify.SoftReserveObserver(page, str(schedule_id)) as observer:
            result = site_selectors.hold_at_application(
                page,
                direct,
                stop_before_submit=self.cfg["hold"]["stop_before_submit"],
                timeout_ms=self.cfg["browser"]["action_timeout_ms"],
                screenshot_path=str(shot),
            )

        if observer.confirmed and not result.held:
            result = site_selectors.HoldResult(
                site_selectors.CONFIRMED,
                f"SPOT HELD — {observer.detail()}\nFinish the steps at {result.url}",
                url=result.url,
                banner=result.banner,
                timings=result.timings,
            )

        if result.held or result.status == site_selectors.UNCERTAIN:
            self._report_hold(shift, result, shot, poll_started)
            return result

        # Direct navigation has historically redirected to login on some
        # sessions even when the normal Apply flow still works. Treat that as a
        # route failure, not immediate proof the shift is lost.
        log.warning("direct application route failed (%s); trying proven click path", result.message[:120])
        parent = Shift(
            id=str(job_id),
            title=shift.title,
            location=shift.location,
            schedule=shift.schedule,
            pay_rate=shift.pay_rate,
            url=f"{self.cfg['site']['base_url'].rstrip('/')}/app#/jobDetail?jobId={job_id}",
            raw={"jobId": str(job_id)},
        )
        original = self.cfg["hold"].get("direct_apply", True)
        self.cfg["hold"]["direct_apply"] = False
        try:
            return super()._hold(parent, poll_started=poll_started)
        finally:
            self.cfg["hold"]["direct_apply"] = original

    def _hold(self, shift, poll_started: float | None = None):
        direct = self._direct_hold(shift, poll_started=poll_started)
        if direct is not None:
            return direct
        return super()._hold(shift, poll_started=poll_started)


# Keep watcher.py's CLI/config/doctor plumbing, but replace the class it creates.
base.Watcher = OptimizedWatcher


if __name__ == "__main__":
    raise SystemExit(base.main())

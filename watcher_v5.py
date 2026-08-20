"""Pre-live watcher: verified sessions + event-driven hold + latency records.

Inheritance remains deliberately additive:
    watcher.Watcher
      -> watcher_v2.ScheduleAwareWatcher
      -> watcher_v3.OptimizedWatcher
      -> watcher_v4.AutoSessionWatcher
      -> watcher_v5.PreLiveWatcher

No private candidate-application request is replayed here.  The fast path still
uses Amazon's frontend and passively observes its reserve response.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path

import fast_hold
import hold_metrics
import schedules as schedules_mod
import site_selectors
import watcher as base
import watcher_v4
from shift_matcher import Shift

log = logging.getLogger("watcher")


class PreLiveWatcher(watcher_v4.AutoSessionWatcher):
    """Final layer used for the real hold validation run."""

    def _start_api_mode(self, browser_cfg: dict) -> None:
        """Prime a persistent live profile from the latest saved verified state.

        Playwright ignores storage_state when launch_persistent_context is used.
        Without this, verify_session.py could prove/save a fresh application
        session while run_watcher.bat initially used older cookies/localStorage
        from browser_profile.  Import the saved state into that live context
        before the polling loop starts; v4's background proof remains the
        authoritative verification/recovery layer afterwards.
        """
        saved_payload = None
        if browser_cfg.get("user_data_dir"):
            try:
                path = Path(self.cfg["browser"]["storage_state"])
                if path.exists():
                    saved_payload = json.loads(path.read_text("utf-8"))
                    cookies = saved_payload.get("cookies") if isinstance(saved_payload, dict) else None
                    if cookies:
                        self.context.add_cookies(cookies)
            except Exception as exc:  # noqa: BLE001
                log.debug("could not prime persistent cookies from saved state: %s", exc)
                saved_payload = None

        super()._start_api_mode(browser_cfg)

        if not (browser_cfg.get("user_data_dir") and isinstance(saved_payload, dict)):
            return

        expected_origin = watcher_v4._origin(self.cfg["site"]["base_url"])
        local_entries = []
        for item in saved_payload.get("origins") or []:
            if watcher_v4._origin(str(item.get("origin") or "")) == expected_origin:
                local_entries.extend(item.get("localStorage") or [])

        try:
            if self.page is not None and local_entries:
                self.page.evaluate(
                    """entries => {
                        for (const item of entries) {
                            if (item && typeof item.name === 'string') {
                                localStorage.setItem(item.name, item.value ?? '');
                            }
                        }
                    }""",
                    local_entries,
                )
                if self.token_source is not None:
                    self.token_source.refresh()
            log.info(
                "primed persistent live context from saved verified state: %d localStorage item(s)",
                len(local_entries),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("saved session cookies loaded but localStorage priming failed: %s", exc)

    def _record_hold_metric(
        self,
        shift: Shift,
        result,
        *,
        poll_started: float | None,
        dispatch_started: float,
        backend_detail: str = "",
    ) -> None:
        raw = shift.raw or {}
        now = time.perf_counter()
        try:
            hold_metrics.append_record(
                job_id=raw.get("jobId") or raw.get("parentJobId") or shift.id,
                schedule_id=raw.get("scheduleId"),
                title=shift.title,
                location=shift.location,
                status=getattr(result, "status", "unknown"),
                message=getattr(result, "message", ""),
                poll_to_dispatch_ms=(
                    (dispatch_started - poll_started) * 1000
                    if poll_started is not None else None
                ),
                total_from_poll_ms=(
                    (now - poll_started) * 1000 if poll_started is not None else None
                ),
                hold_timings=getattr(result, "timings", ()) or (),
                backend_detail=backend_detail,
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("could not append hold timing record: %s", exc)

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
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
        shot = base.SCREENSHOT_DIR / f"hold-{stamp}.png"
        direct = schedules_mod.application_url(
            self.cfg["site"]["base_url"], str(job_id), str(schedule_id)
        )

        dispatch_started = time.perf_counter()
        if poll_started is not None:
            log.info(
                "FAST HOLD dispatch schedule=%s at %.0fms from poll start",
                schedule_id,
                (dispatch_started - poll_started) * 1000,
            )
        else:
            log.info("FAST HOLD dispatch schedule=%s", schedule_id)

        result, backend_detail = fast_hold.hold(
            page,
            direct,
            str(schedule_id),
            base_url=self.cfg["site"]["base_url"],
            stop_before_submit=self.cfg["hold"]["stop_before_submit"],
            timeout_ms=self.cfg["browser"]["action_timeout_ms"],
            screenshot_path=str(shot),
        )
        self._record_hold_metric(
            shift,
            result,
            poll_started=poll_started,
            dispatch_started=dispatch_started,
            backend_detail=backend_detail,
        )

        if result.held or result.status == site_selectors.UNCERTAIN:
            self._report_hold(shift, result, shot, poll_started)
            return result

        # Keep the original click path as a compatibility fallback.  Disable
        # direct_apply only for this call so v3._hold falls through to v2's
        # proven card/detail/schedule flow instead of recursively retrying this
        # direct route.
        log.warning(
            "fast direct route failed (%s); trying original click fallback",
            (result.message or "")[:140],
        )
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
        fallback_started = time.perf_counter()
        try:
            fallback = super()._hold(parent, poll_started=poll_started)
        finally:
            self.cfg["hold"]["direct_apply"] = original

        if fallback is not None:
            # Separate record so the first live test tells us whether the fast
            # path won or the compatibility path had to rescue it.
            fallback_shift = Shift(
                id=shift.id,
                title=shift.title,
                location=shift.location,
                schedule=shift.schedule,
                pay_rate=shift.pay_rate,
                raw=dict(raw),
            )
            self._record_hold_metric(
                fallback_shift,
                fallback,
                poll_started=poll_started,
                dispatch_started=fallback_started,
                backend_detail="compatibility fallback",
            )
        return fallback

    def _stop_session_worker(self) -> None:
        worker = getattr(self, "session_worker", None)
        if worker is None or worker.poll() is not None:
            return
        try:
            worker.terminate()
            worker.wait(timeout=3)
        except Exception:
            try:
                worker.kill()
            except Exception:
                pass
        finally:
            self.session_worker = None

    def run(self, once: bool = False) -> int:
        try:
            return super().run(once=once)
        finally:
            self._stop_session_worker()


base.Watcher = PreLiveWatcher


if __name__ == "__main__":
    raise SystemExit(base.main())

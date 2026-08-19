"""Schedule-aware launcher for the Amazon shift watcher.

This keeps the proven watcher/session/login machinery intact while fixing the
race-critical gaps discovered from live network captures:

* track bookable scheduleIds, not only jobIds;
* retry a failed hold on the next poll without repeating Telegram alerts;
* apply schedule_preferences before attempting a reservation;
* skip the redundant job-detail navigation when a scheduleId is already known;
* keep the browser-driven Create Application flow as the reservation trigger.

Run this exactly like watcher.py. run_watcher.bat points here on the improvement
branch so reverting is a one-line change.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path

import schedules as schedules_mod
import site_selectors
import watcher as base
from shift_matcher import Shift

log = logging.getLogger("watcher")


class ScheduleAwareWatcher(base.Watcher):
    """Watcher whose unit of work is a reservable schedule, not a job card."""

    @staticmethod
    def _schedule_key(schedule) -> str:
        return f"schedule:{schedule.id}"

    @staticmethod
    def _alert_key(schedule) -> str:
        return f"alert:schedule:{schedule.id}"

    def _schedule_is_acceptable(self, schedule) -> tuple[bool, str]:
        """Apply the existing schedule_preferences to an API schedule card."""
        prefs = self.cfg.get("schedule_preferences") or {}
        if not prefs:
            return True, "acceptable"

        parsed = schedules_mod.parse_card_text(schedule.text or "")
        if schedule.hours_per_week is not None:
            parsed["hours_per_week"] = schedule.hours_per_week
        if schedule.pay_rate is not None:
            parsed["pay_rate"] = schedule.pay_rate
        parsed["text"] = schedule.text or parsed.get("text", "")
        return schedules_mod.card_is_acceptable(parsed, prefs)

    def _schedule_shift(self, job, schedule) -> Shift:
        """Represent one concrete schedule with the normal Shift interface."""
        raw = dict(schedule.raw or {})
        raw.setdefault("jobId", schedule.job_id or job.id)
        raw.setdefault("scheduleId", schedule.id)
        raw["parentJobId"] = job.id
        raw["laborDemandAvailableCount"] = schedule.available

        return Shift(
            # scheduleId is the identity now. The parent job id stays in raw.
            id=schedule.id,
            title=schedule.title or job.title,
            location=schedule.location or job.location,
            schedule=schedule.text or job.schedule,
            pay_rate=schedule.pay_rate if schedule.pay_rate is not None else job.pay_rate,
            url=None,
            raw=raw,
        )

    def _expand_job_to_schedules(self, job) -> list[Shift] | None:
        """Return acceptable, currently bookable schedules for one matched job.

        None means schedule lookup failed and the caller should fall back to the
        old job-level behavior. [] means lookup succeeded but nothing can be
        reserved right now.
        """
        if self.mode != "api" or self.api_client is None or not job.id:
            return None

        try:
            schedules = self.api_client.fetch_schedules(job.id)
        except Exception as exc:  # noqa: BLE001 - keep the original fallback alive
            log.warning("schedule lookup failed for %s: %s", job.id, exc)
            return None

        out: list[Shift] = []
        for schedule in schedules_mod.bookable(schedules):
            ok, reason = self._schedule_is_acceptable(schedule)
            if not ok:
                log.debug("skip schedule %s (%s)", schedule.id, reason)
                continue
            out.append(self._schedule_shift(job, schedule))
        return out

    def _candidate_key(self, shift: Shift) -> tuple[str, str]:
        """Persistent done/alert keys for either a schedule or fallback job."""
        schedule_id = (shift.raw or {}).get("scheduleId")
        if schedule_id:
            return f"done:schedule:{schedule_id}", f"alert:schedule:{schedule_id}"
        return f"done:{shift.stable_id}", f"alert:{shift.stable_id}"

    def poll_once(self) -> None:  # noqa: C901 - deliberately mirrors base critical path
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
        candidates: list[Shift] = []

        for job in jobs:
            matched, reason = self.matcher.matches(job)
            if not matched:
                rejected.append(f"{job.summary()} [{reason}]")
                continue

            expanded = self._expand_job_to_schedules(job)
            if expanded is None:
                # DOM mode, or a transient schedule-query failure: preserve the
                # existing behavior instead of turning one failed query into a
                # blind watcher.
                done_key, _ = self._candidate_key(job)
                if not self.state.has_seen(done_key):
                    candidates.append(job)
                continue

            # Crucial difference from the old watcher: an already-seen job does
            # not hide a newly available schedule on that same posting.
            for schedule_shift in expanded:
                done_key, _ = self._candidate_key(schedule_shift)
                if self.state.has_seen(done_key):
                    continue
                candidates.append(schedule_shift)

        if rejected:
            log.info(
                "%d posting(s) seen but filtered out: %s",
                len(rejected), " | ".join(rejected[:5]),
            )

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

        # Log every newly-alerted schedule once. Alert state and reservation
        # state are intentionally separate: a transient failed hold must be
        # retried without sending the same Telegram message every two seconds.
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
            log.info(
                "dry run — not clicking" if self.dry_run else "hold disabled — alert only"
            )
            return

        hold_cap = max(1, int(self.cfg["hold"].get("max_per_poll", 1)))
        job_attempts = max(1, int(self.cfg["hold"].get("job_attempts", 3)))
        budget = float(self.cfg["hold"].get("attempt_budget_seconds", 45))

        # Best candidates first, including previously-alerted schedules whose
        # last reservation attempt failed. Only the notification is deduped.
        to_try = candidates[:max(job_attempts, hold_cap)]
        for shift in to_try[:hold_cap]:
            _, alert_key = self._candidate_key(shift)
            if any(s.id == shift.id for s in newly_alerted):
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

                if outcome is None:
                    # Preserve the base watcher's conservative behavior for a
                    # caller that replaced _hold.
                    self.state.mark_seen(done_key, shift.summary())
                    held += 1
                    continue

                if outcome.held or outcome.status == site_selectors.UNCERTAIN:
                    self.state.mark_seen(done_key, shift.summary())
                    held += 1
                    continue

                # FAILED deliberately remains *not done*. If the schedule is
                # still returned with capacity on the next poll, retry it. This
                # fixes the old mark-before-acting behavior that retired a job
                # for 72 hours after one transient failure.
                log.info("reservation failed; schedule remains retryable next poll: %s", shift.id)
                if not outcome.worth_retrying():
                    # The next different schedule may still work after a
                    # schedule-specific loss; but global failures (login,
                    # configuration) should not burn the whole batch.
                    break
        finally:
            self.holding = False
            self.state.save()

        # Alert other new matches after the reservation critical path.
        alerted_ids = {s.id for s in newly_alerted[:alert_cap]}
        for shift in newly_alerted:
            if shift.id in alerted_ids and shift not in to_try[:hold_cap]:
                self.notify_async(self.notifier.notify_shift, shift, dry_run=True)
        self._send_digest(max(0, len(newly_alerted) - alert_cap))

    def _hold(self, shift, poll_started: float | None = None):
        """Use the direct application route immediately when scheduleId is known.

        base.Watcher._hold first navigates to shift.url/job search and only then
        calculates the direct application URL. That extra navigation is pure
        latency for an API schedule because jobId + scheduleId are already in
        hand. Skip it here, while retaining the proven browser-driven consent
        and Create Application flow.
        """
        raw = shift.raw or {}
        schedule_id = raw.get("scheduleId")
        job_id = raw.get("jobId") or raw.get("parentJobId")
        if not (schedule_id and job_id and self.cfg["hold"].get("direct_apply", True)):
            return super()._hold(shift, poll_started=poll_started)

        if self.session_ok is False:
            return super()._hold(shift, poll_started=poll_started)

        missing = site_selectors.unconfigured_hold()
        if missing:
            return super()._hold(shift, poll_started=poll_started)

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
        result = site_selectors.hold_at_application(
            page,
            direct,
            stop_before_submit=self.cfg["hold"]["stop_before_submit"],
            timeout_ms=self.cfg["browser"]["action_timeout_ms"],
            screenshot_path=str(shot),
        )
        self._report_hold(shift, result, shot, poll_started)
        return result


# watcher.main resolves its global Watcher at call time. Replacing it lets all
# existing CLI flags, doctor checks, config loading and error handling remain
# exactly as before.
base.Watcher = ScheduleAwareWatcher


if __name__ == "__main__":
    raise SystemExit(base.main())

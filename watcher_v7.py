"""Known-job lifecycle detection attached to the proven v6 hold pipeline."""

from __future__ import annotations

import logging
import time

import job_lifecycle
import schedules as schedules_mod
import watcher as base
import watcher_v6

log = logging.getLogger("watcher")


class LifecycleWatcher(watcher_v6.HoldReadyWatcher):
    def __init__(self, cfg: dict, live_override: bool = False) -> None:
        super().__init__(cfg, live_override=live_override)
        lifecycle = cfg.get("lifecycle_monitor") or {}
        self.lifecycle_enabled = bool(
            lifecycle.get("enabled", False)
            and lifecycle.get("known_jobs")
            and self.mode == "api"
        )
        self.lifecycle_interval = float(lifecycle.get("interval_seconds", 2.0))
        self.lifecycle_next_poll = 0.0
        self.lifecycle_candidates = []
        self.lifecycle_failures = 0
        self.lifecycle_backoff_until = 0.0
        self.lifecycle_notify_unposted = bool(lifecycle.get("notify_unposted", True))
        self.lifecycle_notify_unconfirmed_posted = bool(
            lifecycle.get("notify_posted_without_capacity", True)
        )
        self.lifecycle_monitor = job_lifecycle.LifecycleMonitor(
            lifecycle.get("known_jobs") or [],
            state_path=lifecycle.get("state_path", "state/job_lifecycle.json"),
            events_path=lifecycle.get("events_path", "state/job_lifecycle_events.jsonl"),
        ) if self.lifecycle_enabled else None
        if self.lifecycle_enabled:
            log.info(
                "known-job lifecycle detector armed for %d job id(s) at %.2fs cadence",
                len(self.lifecycle_monitor.known_jobs), self.lifecycle_interval,
            )

    def _lifecycle_tick(self) -> None:
        if not self.lifecycle_enabled or self.lifecycle_monitor is None or self.api_client is None:
            return
        now = time.monotonic()
        if now < self.lifecycle_next_poll or now < self.lifecycle_backoff_until:
            return
        self.lifecycle_next_poll = now + self.lifecycle_interval
        try:
            candidates, events = self.lifecycle_monitor.poll(self.api_client)
            for candidate in candidates:
                raw = candidate.raw or {}
                candidate.url = schedules_mod.application_url(
                    self.cfg["site"]["base_url"],
                    str(raw.get("jobId") or raw.get("parentJobId")),
                    str(raw.get("scheduleId")),
                )
            self.lifecycle_candidates = candidates
            self.lifecycle_failures = 0
            capacity_jobs = {
                event["job"].job_id
                for event in events
                if event.get("event") == "SCHEDULE_CAPACITY_AVAILABLE"
            }
            for event in events:
                kind = event.get("event")
                job = event.get("job")
                if kind == "JOB_POSTED":
                    log.info("LIFECYCLE POSTED: %s %s", job.site_code, job.job_id)
                    if (
                        self.lifecycle_notify_unconfirmed_posted
                        and job.job_id not in capacity_jobs
                    ):
                        self.notify_async(
                            self.notifier.notify_job_posted_without_capacity,
                            job,
                            self._known_job_url(job.job_id),
                        )
                elif kind == "JOB_UNPOSTED":
                    duration = event.get("postedDurationSeconds")
                    log.info(
                        "LIFECYCLE UNPOSTED: %s %s after %ss",
                        job.site_code, job.job_id,
                        "unknown" if duration is None else f"{duration:.1f}",
                    )
                    if self.lifecycle_notify_unposted:
                        self.notify_async(
                            self.notifier.notify_job_unposted,
                            job, duration, self._known_job_url(job.job_id),
                        )
                elif kind == "SCHEDULE_CAPACITY_AVAILABLE":
                    schedule = event.get("schedule")
                    log.info(
                        "LIFECYCLE CAPACITY: job=%s schedule=%s available=%s",
                        job.job_id, schedule.id, event.get("capacity"),
                    )
        except Exception as exc:  # noqa: BLE001
            # Never include the request exception text: a transport's call log
            # can contain headers.  Public search and holding keep running.
            self.lifecycle_failures += 1
            log.warning(
                "known-job lifecycle poll failed (%s, %d consecutive); public search continues",
                type(exc).__name__, self.lifecycle_failures,
            )
            if self.lifecycle_failures >= 3:
                self.lifecycle_backoff_until = now + 60.0
                self.lifecycle_failures = 0
                log.warning("known-job lifecycle detector backing off for 60s")

    def _known_job_url(self, job_id: str) -> str:
        return (
            f"{self.cfg['site']['base_url'].rstrip('/')}/app#/jobDetail"
            f"?jobId={job_id}&locale=en-CA"
        )

    def _fetch_shifts(self):
        self._lifecycle_tick()
        return super()._fetch_shifts()

    def _batch_expand(self, jobs):
        public = super()._batch_expand(jobs)
        known = []
        for candidate in self.lifecycle_candidates:
            matched, reason = self.matcher.matches(candidate)
            if not matched:
                log.debug("skip lifecycle schedule %s (%s)", candidate.id, reason)
                continue
            schedule = schedules_mod.Schedule(candidate.raw)
            ok, reason = self._schedule_is_acceptable(schedule)
            if ok:
                known.append(candidate)
            else:
                log.debug("skip lifecycle schedule %s (%s)", candidate.id, reason)

        if public is None:
            return known or None
        merged = []
        seen = set()
        # Known-job capacity is origin-fresh evidence, so it wins ties and is
        # dispatched first without changing the hold implementation itself.
        for candidate in [*known, *public]:
            key = ((candidate.raw or {}).get("scheduleId") or candidate.id)
            if key not in seen:
                merged.append(candidate)
                seen.add(key)
        return merged

    def _candidate_key(self, shift):
        raw = shift.raw or {}
        if raw.get("lifecycleSource") == "known_job" and raw.get("scheduleId"):
            epoch = str(raw.get("lifecycleEpoch") or "current")
            schedule_id = str(raw["scheduleId"])
            return (
                f"done:lifecycle:{schedule_id}:{epoch}",
                f"alert:lifecycle:{schedule_id}:{epoch}",
            )
        return super()._candidate_key(shift)


base.Watcher = LifecycleWatcher


if __name__ == "__main__":
    raise SystemExit(base.main())

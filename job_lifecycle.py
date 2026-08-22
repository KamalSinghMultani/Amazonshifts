"""Persistent known-job lifecycle monitoring.

The public job search is still the discovery/fallback path. This module watches
already-known public job ids with Amazon's own getJobDetail query, then asks for
their schedules only while the job is POSTED. A schedule is emitted as a live
candidate only when Amazon reports laborDemandHardMatchCount > 0.

laborDemandAvailableCount is retained as metadata only; it is not the validity
gate for lifecycle candidates.

State and events intentionally contain public job/schedule facts only. Request
headers, cookies, tokens and identity/KYC parameters never enter this module.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import schedule_batch
from shift_matcher import Shift


JOB_FIELDS = """
      jobId
      postingStatus
      locationName
      siteId
      jobTitle
      employmentType
      jobType
      compliancePayRangeMin
      compliancePayRangeMax
      __typename
"""


class JobDetailGraphQLError(RuntimeError):
    """Amazon rejected the public job-detail document.

    The response text is deliberately not retained because GraphQL diagnostics
    can echo request values. The exception type alone is safe and sufficient
    for the watcher log and backoff classifier.
    """


@dataclass(frozen=True)
class KnownJob:
    job_id: str
    site_code: str = ""
    location: str = ""


def normalize_known_jobs(values: Iterable[dict | KnownJob]) -> list[KnownJob]:
    out: list[KnownJob] = []
    seen: set[str] = set()
    for value in values or []:
        if isinstance(value, KnownJob):
            item = value
        elif isinstance(value, dict):
            item = KnownJob(
                job_id=str(value.get("job_id") or "").strip(),
                site_code=str(value.get("site_code") or "").strip(),
                location=str(value.get("location") or "").strip(),
            )
        else:
            continue
        if item.job_id and item.job_id not in seen:
            out.append(item)
            seen.add(item.job_id)
    return out


def build_request(job_id: str, *, locale: str = "en-CA") -> dict:
    """Build the exact one-job document shipped by Amazon's current frontend."""
    return {
        "operationName": "getJobDetail",
        "variables": {
            "getJobDetailRequest": {"jobId": str(job_id), "locale": locale}
        },
        "query": (
            "query getJobDetail($getJobDetailRequest: GetJobDetailRequest!) {\n"
            "  getJobDetail(getJobDetailRequest: $getJobDetailRequest) {\n"
            + JOB_FIELDS
            + "  }\n}"
        ),
    }


def parse(payload: dict) -> dict | None:
    data = payload.get("data") if isinstance(payload, dict) else None
    data = data if isinstance(data, dict) else {}
    node = data.get("getJobDetail")
    return dict(node) if isinstance(node, dict) else None


def fetch(client, known_jobs: Iterable[KnownJob]) -> dict[str, dict | None]:
    """Fetch jobs independently so one missing job cannot poison every node.

    Live evidence on 2026-08-21 showed that aliases returned partial data plus
    GraphQL errors even though every corrected id succeeded with the frontend's
    one-job document. Keep transport/HTTP failures authoritative, but treat a
    resolver error for one public job id as inconclusive for that id only.
    """
    out: dict[str, dict | None] = {}
    locale = getattr(client, "locale", None) or "en-CA"
    for job in known_jobs:
        request = build_request(job.job_id, locale=locale)
        response = client._post_json(request)
        if isinstance(response, dict) and response.get("errors"):
            out[job.job_id] = None
            continue
        out[job.job_id] = parse(response)
    return out


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _positive_count(value) -> int | float | None:
    # bool is an int in Python but is not credible count evidence.
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and value > 0:
        return value
    return None


class LifecycleMonitor:
    """Track transitions and return every currently hard-match-valid candidate."""

    def __init__(
        self,
        known_jobs: Iterable[dict | KnownJob],
        *,
        state_path: str | Path,
        events_path: str | Path,
        now_fn=_utc_now,
    ) -> None:
        self.known_jobs = normalize_known_jobs(known_jobs)
        self.by_id = {item.job_id: item for item in self.known_jobs}
        self.state_path = Path(state_path)
        self.events_path = Path(events_path)
        self.now_fn = now_fn
        self.state = self._load_state()
        self.last_observed_jobs = 0
        self.last_attempted_jobs = 0
        self._cursor = 0

    def _load_state(self) -> dict:
        try:
            value = json.loads(self.state_path.read_text("utf-8"))
            if isinstance(value, dict):
                value.setdefault("jobs", {})
                return value
        except (OSError, ValueError):
            pass
        return {"version": 1, "jobs": {}}

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temp.write_text(json.dumps(self.state, indent=2, sort_keys=True), "utf-8")
        os.replace(temp, self.state_path)

    def _event(self, kind: str, *, at: str, job: KnownJob, **fields) -> None:
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        event = {
            "at": at,
            "event": kind,
            "jobId": job.job_id,
            "siteCode": job.site_code,
            "location": job.location,
            **fields,
        }
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True) + "\n")

    def _jobs_for_poll(self, max_jobs: int | None) -> list[KnownJob]:
        if not self.known_jobs or max_jobs is None or int(max_jobs) <= 0:
            return list(self.known_jobs)
        count = min(len(self.known_jobs), max(1, int(max_jobs)))
        selected = [
            self.known_jobs[(self._cursor + offset) % len(self.known_jobs)]
            for offset in range(count)
        ]
        self._cursor = (self._cursor + count) % len(self.known_jobs)
        return selected

    def poll(
        self,
        client,
        *,
        max_jobs: int | None = None,
    ) -> tuple[list[Shift], list[dict]]:
        now = self.now_fn()
        at = _stamp(now)
        jobs = self._jobs_for_poll(max_jobs)
        self.last_attempted_jobs = len(jobs)
        details = fetch(client, jobs)
        posted_ids: list[str] = []
        transition_events: list[dict] = []
        observed_jobs = 0

        for job in jobs:
            detail = details.get(job.job_id)
            if not isinstance(detail, dict):
                # Missing/inconclusive data never overwrites the last truth.
                continue
            status = str(detail.get("postingStatus") or "").upper()
            if status not in {"POSTED", "UNPOSTED"}:
                continue
            observed_jobs += 1

            record = self.state["jobs"].setdefault(job.job_id, {})
            previous = record.get("status")
            event: dict | None = None
            if status == "POSTED" and previous != "POSTED":
                record["postedSince"] = at
                record["epoch"] = at
                event = {"event": "JOB_POSTED", "status": status, "previousStatus": previous}
                self._event("JOB_POSTED", at=at, job=job, previousStatus=previous)
            elif status == "UNPOSTED" and previous == "POSTED":
                duration = None
                try:
                    start = datetime.fromisoformat(str(record.get("postedSince", "")).replace("Z", "+00:00"))
                    duration = max(0.0, (now - start).total_seconds())
                except (TypeError, ValueError):
                    pass
                event = {
                    "event": "JOB_UNPOSTED",
                    "status": status,
                    "previousStatus": previous,
                    "postedDurationSeconds": duration,
                }
                self._event(
                    "JOB_UNPOSTED", at=at, job=job,
                    postedDurationSeconds=duration,
                )
                record["schedules"] = {}

            record.update({
                "status": status,
                "lastObservedAt": at,
                "title": str(detail.get("jobTitle") or record.get("title") or ""),
                "location": str(detail.get("locationName") or job.location),
                "siteCode": job.site_code,
            })
            if event:
                transition_events.append({**event, "job": job, "detail": detail})
            if status == "POSTED":
                posted_ids.append(job.job_id)

        if jobs and observed_jobs == 0:
            raise JobDetailGraphQLError("job detail response contained no usable lifecycle nodes")
        self.last_observed_jobs = observed_jobs

        schedule_map = schedule_batch.fetch(client, posted_ids, chunk_size=20) if posted_ids else {}
        candidates: list[Shift] = []
        for job_id in posted_ids:
            job = self.by_id[job_id]
            record = self.state["jobs"].setdefault(job_id, {})
            prior_schedules = record.setdefault("schedules", {})
            seen_this_poll: set[str] = set()
            for schedule in schedule_map.get(job_id, []):
                if not schedule.id:
                    continue
                seen_this_poll.add(schedule.id)

                # The validity gate requested for CA schedules is strictly the
                # hard-match count. Availability is recorded, but it does not
                # decide whether the schedule becomes a hold candidate.
                hard_match = _positive_count(schedule.hard_match)
                previous_hard_match = prior_schedules.get(schedule.id, {}).get("hardMatchCount")
                prior_schedules[schedule.id] = {
                    "hardMatchCount": (
                        hard_match if hard_match is not None else schedule.hard_match
                    ),
                    "availableCount": schedule.available,
                    "lastObservedAt": at,
                }
                if hard_match is None:
                    continue

                # Keep the historical event name for compatibility with v7's
                # notification flow, but the numeric signal is now hard-match
                # count rather than laborDemandAvailableCount.
                if _positive_count(previous_hard_match) is None:
                    self._event(
                        "SCHEDULE_CAPACITY_AVAILABLE", at=at, job=job,
                        scheduleId=schedule.id, capacity=hard_match,
                        hardMatchCount=hard_match,
                        availableCount=schedule.available,
                        epoch=record.get("epoch"),
                    )
                    transition_events.append({
                        "event": "SCHEDULE_CAPACITY_AVAILABLE",
                        "job": job,
                        "schedule": schedule,
                        "capacity": hard_match,
                        "hardMatchCount": hard_match,
                        "availableCount": schedule.available,
                    })

                raw = dict(schedule.raw or {})
                raw.update({
                    "jobId": job_id,
                    "parentJobId": job_id,
                    "scheduleId": schedule.id,
                    "laborDemandAvailableCount": schedule.available,
                    "laborDemandHardMatchCount": hard_match,
                    "lifecycleSource": "known_job",
                    "lifecycleEpoch": record.get("epoch") or at,
                    "postingStatus": "POSTED",
                    "siteCode": job.site_code,
                })
                candidates.append(Shift(
                    id=schedule.id,
                    title=schedule.title or record.get("title") or "Amazon Warehouse Associate",
                    location=schedule.location or record.get("location") or job.location,
                    schedule=schedule.text,
                    pay_rate=schedule.pay_rate,
                    raw=raw,
                ))

            # Absence is explicit loss of schedule-level validity for our next
            # rising-edge decision; it is never interpreted as a hard match.
            for schedule_id in set(prior_schedules) - seen_this_poll:
                prior_schedules[schedule_id] = {
                    "hardMatchCount": None,
                    "availableCount": None,
                    "lastObservedAt": at,
                }

        self._save()
        return candidates, transition_events

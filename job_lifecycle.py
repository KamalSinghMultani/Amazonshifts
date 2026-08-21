"""Persistent known-job lifecycle monitoring.

The public job search is still the discovery/fallback path.  This module watches
already-known public job ids with Amazon's own getJobDetail query, then asks for
their schedules only while the job is POSTED.  A schedule is emitted as a live
candidate only when Amazon reports a strictly positive capacity.

State and events intentionally contain public job/schedule facts only.  Request
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


def build_request(job_ids: Iterable[str], *, locale: str = "en-CA") -> tuple[dict, list[str]]:
    ids = list(dict.fromkeys(str(job_id) for job_id in job_ids if job_id))
    variables: dict[str, dict] = {}
    declarations: list[str] = []
    selections: list[str] = []
    for index, job_id in enumerate(ids):
        var = f"r{index}"
        alias = f"j{index}"
        variables[var] = {"jobId": job_id, "locale": locale}
        declarations.append(f"${var}: GetJobDetailRequest!")
        selections.append(
            f"""{alias}: getJobDetail(getJobDetailRequest: ${var}) {{
{JOB_FIELDS}
    }}"""
        )
    query = "query batchJobDetail"
    if declarations:
        query += "(" + ", ".join(declarations) + ")"
    query += " {\n  " + "\n  ".join(selections) + "\n}"
    return {
        "operationName": "batchJobDetail",
        "variables": variables,
        "query": query,
    }, ids


def parse(payload: dict, job_ids: list[str]) -> dict[str, dict | None]:
    data = payload.get("data") if isinstance(payload, dict) else None
    data = data if isinstance(data, dict) else {}
    out: dict[str, dict | None] = {}
    for index, job_id in enumerate(job_ids):
        node = data.get(f"j{index}")
        out[job_id] = dict(node) if isinstance(node, dict) else None
    return out


def fetch(client, known_jobs: Iterable[KnownJob], *, chunk_size: int = 20) -> dict[str, dict | None]:
    ids = [job.job_id for job in known_jobs]
    out: dict[str, dict | None] = {}
    chunk_size = max(1, int(chunk_size))
    for start in range(0, len(ids), chunk_size):
        chunk = ids[start:start + chunk_size]
        request, ordered = build_request(
            chunk, locale=getattr(client, "locale", None) or "en-CA"
        )
        response = client._post_json(request)
        if isinstance(response, dict) and response.get("errors"):
            # Keep GraphQL diagnostics out of logs; backend messages can echo
            # request details. The watcher reports only this sanitized type.
            raise RuntimeError("job detail GraphQL response contained errors")
        out.update(parse(response, ordered))
    return out


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _positive_capacity(value) -> int | float | None:
    # bool is an int in Python but is not credible capacity evidence.
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and value > 0:
        return value
    return None


class LifecycleMonitor:
    """Track transitions and return every currently capacity-confirmed candidate."""

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

    def poll(self, client) -> tuple[list[Shift], list[dict]]:
        now = self.now_fn()
        at = _stamp(now)
        details = fetch(client, self.known_jobs)
        posted_ids: list[str] = []
        transition_events: list[dict] = []

        for job in self.known_jobs:
            detail = details.get(job.job_id)
            if not isinstance(detail, dict):
                # Missing/inconclusive data never overwrites the last truth.
                continue
            status = str(detail.get("postingStatus") or "").upper()
            if status not in {"POSTED", "UNPOSTED"}:
                continue

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
                capacity = _positive_capacity(schedule.available)
                previous_capacity = prior_schedules.get(schedule.id, {}).get("capacity")
                prior_schedules[schedule.id] = {
                    "capacity": capacity if capacity is not None else schedule.available,
                    "lastObservedAt": at,
                }
                if capacity is None:
                    continue
                if _positive_capacity(previous_capacity) is None:
                    self._event(
                        "SCHEDULE_CAPACITY_AVAILABLE", at=at, job=job,
                        scheduleId=schedule.id, capacity=capacity,
                        epoch=record.get("epoch"),
                    )
                    transition_events.append({
                        "event": "SCHEDULE_CAPACITY_AVAILABLE",
                        "job": job,
                        "schedule": schedule,
                        "capacity": capacity,
                    })

                raw = dict(schedule.raw or {})
                raw.update({
                    "jobId": job_id,
                    "parentJobId": job_id,
                    "scheduleId": schedule.id,
                    "laborDemandAvailableCount": capacity,
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

            # Absence is explicit loss of schedule-level availability for our
            # next rising-edge decision; it is never interpreted as capacity.
            for schedule_id in set(prior_schedules) - seen_this_poll:
                prior_schedules[schedule_id] = {"capacity": None, "lastObservedAt": at}

        self._save()
        return candidates, transition_events

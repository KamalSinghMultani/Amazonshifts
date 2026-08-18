"""Schedules for a job, and the direct route into an application.

WHY THIS EXISTS
---------------
Job cards say "3 shifts available". The actual shifts — their days, their
hours, and the scheduleId that identifies them — live behind a second query,
searchScheduleCards, which needs a jobId.

Two things follow, and the second is the point:

1. Real shift times become filterable. A card only offers "Duration:
   Seasonal"; a schedule says "Thu, Fri, Sat, Sun 1:20 AM - 11:50 AM".

2. The application can be opened DIRECTLY. Holding a slot normally means
   clicking a card, waiting for the detail page, opening the schedule flyout,
   pressing Apply, and following the tab it spawns — five page loads and the
   two flakiest selectors in the project. With a scheduleId the whole lot
   collapses into one navigation:

       /application/?jobId=JOB-CA-…&page=pre-consent&scheduleId=SCH-CA-…

Confirmed live 2026-08-18 by capturing what the site itself calls.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

log = logging.getLogger(__name__)

SCHEDULE_QUERY = """query searchScheduleCards($searchScheduleRequest: SearchScheduleRequest!) {
  searchScheduleCards(searchScheduleRequest: $searchScheduleRequest) {
    nextToken
    scheduleCards {
      jobId
      scheduleId
      externalJobTitle
      city
      state
      siteId
      scheduleText
      scheduleType
      employmentType
      hoursPerWeek
      totalPayRate
      firstDayOnSite
      laborDemandAvailableCount
      __typename
    }
    __typename
  }
}"""


def build_request(job_id: str, *, country: str = "Canada", locale: str = "en-CA",
                  page_size: int = 100) -> dict:
    """The site's own request, trimmed to the fields we use.

    jobId is REQUIRED — tested: without it the endpoint answers
    ERR_RESPONSE_JC_SERVICE, so schedules cannot be watched globally and job
    cards remain the way in.
    """
    return {
        "operationName": "searchScheduleCards",
        "variables": {"searchScheduleRequest": {
            "jobId": job_id,
            "locale": locale,
            "country": country,
            "keyWords": "",
            "equalFilters": [],
            "containFilters": [{"key": "isPrivateSchedule", "val": ["false"]}],
            "rangeFilters": [],
            "orFilters": [],
            # Deliberately empty: the site pins today's date here, which would
            # rot in a config file at the next midnight.
            "dateFilters": [],
            "sorters": [],
            "pageSize": page_size,
            "consolidateSchedule": True,
        }},
        "query": SCHEDULE_QUERY,
    }


class Schedule:
    """One bookable shift."""

    def __init__(self, raw: dict) -> None:
        self.raw = raw or {}
        self.job_id = self.raw.get("jobId") or ""
        self.id = self.raw.get("scheduleId") or ""
        self.title = self.raw.get("externalJobTitle") or ""
        self.city = self.raw.get("city") or ""
        self.state = self.raw.get("state") or ""
        self.site_id = self.raw.get("siteId") or ""
        self.text = self.raw.get("scheduleText") or ""
        self.type = self.raw.get("scheduleType") or ""
        self.employment_type = self.raw.get("employmentType") or ""
        self.hours_per_week = self.raw.get("hoursPerWeek")
        self.pay_rate = self.raw.get("totalPayRate")
        self.first_day = self.raw.get("firstDayOnSite") or ""
        self.available = self.raw.get("laborDemandAvailableCount")

    @property
    def location(self) -> str:
        return ", ".join(part for part in (self.city, self.state) if part)

    def summary(self) -> str:
        bits = [self.title or "(untitled)"]
        if self.location:
            bits.append(self.location)
        if self.site_id:
            bits.append(self.site_id)
        if self.text:
            bits.append(self.text)
        if self.pay_rate is not None:
            bits.append(f"${self.pay_rate}/hr")
        return " — ".join(bits)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Schedule {self.id} {self.summary()[:50]}>"


def parse(payload: Any) -> list[Schedule]:
    """Schedules out of a GraphQL response. A schema change yields nothing
    rather than raising — the watcher must survive Amazon's refactors."""
    try:
        cards = payload["data"]["searchScheduleCards"]["scheduleCards"]
    except (KeyError, TypeError):
        log.warning("no scheduleCards in the response")
        return []
    if not isinstance(cards, list):
        return []
    return [Schedule(card) for card in cards if isinstance(card, dict)]


def bookable(schedules: list[Schedule]) -> list[Schedule]:
    """Drop schedules with no capacity left.

    laborDemandAvailableCount is the site's own count of remaining places.
    Zero means the shift is visible but already gone — attempting it would
    spend the seconds that matter on something unwinnable.
    """
    out = []
    for schedule in schedules:
        if schedule.available is None or schedule.available > 0:
            out.append(schedule)
        else:
            log.debug("skipping %s — no places left", schedule.id)
    return out


def application_url(base_url: str, job_id: str, schedule_id: str) -> str:
    """The page Apply would have opened, addressable directly.

    Confirmed live: clicking Apply navigates to exactly this, which is what
    makes the click path skippable.
    """
    return (
        f"{base_url.rstrip('/')}/application/"
        f"?jobId={quote(job_id)}&page=pre-consent&scheduleId={quote(schedule_id)}"
    )

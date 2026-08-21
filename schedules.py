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
import re
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


def identity_verification_url(base_url: str, job_id: str, schedule_id: str) -> str:
    """Safe manual Canada liveness route without KYC tracking parameters.

    Amazon may append a private trackingId while routing into remoteKYC.  The
    Telegram alert must never copy that URL.  The public job/schedule pair is
    sufficient for Amazon to resume the signed-in candidate's own flow.
    """
    job = quote(str(job_id), safe="")
    schedule = quote(str(schedule_id), safe="")
    return (
        f"{base_url.rstrip('/')}/application/ca/"
        f"?jobId={job}&scheduleId={schedule}"
        f"#/liveness-check?jobId={job}&scheduleId={schedule}"
    )

# ── choosing WHICH schedule, from the flyout ────────────────────────────────
# The flyout renders one card per schedule and the hold used to click the
# first. Confirmed live 2026-08-18, a single job offered:
#
#   $24.00/hr | Featured | Schedule (26h per week) | Wed, Thu, Fri, Sat 9:30 PM - 4:00 AM
#   $24.00/hr | Featured | Schedule (26h per week) | Sun, Mon, Tue, Wed 9:30 PM - 4:00 AM
#
# Same pay, same hours, different days. Whichever renders first is not a
# choice, it is an accident — and you have to work whichever one it takes.

DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

HOURS_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*h(?:ours?)?\s*per\s*week", re.I)
PAY_PATTERN = re.compile(r"\$\s*(\d+(?:\.\d+)?)", re.I)
TIME_RANGE = re.compile(
    r"(\d{1,2}:\d{2}\s*[AP]M)\s*[-–—]\s*(\d{1,2}:\d{2}\s*[AP]M)", re.I
)


def parse_card_text(text: str) -> dict:
    """Pull the facts out of one schedule card's visible text."""
    low = (text or "").lower()

    hours = HOURS_PATTERN.search(low)
    pay = PAY_PATTERN.search(low)
    times = TIME_RANGE.search(text or "")

    return {
        "hours_per_week": float(hours.group(1)) if hours else None,
        "pay_rate": float(pay.group(1)) if pay else None,
        "days": [day for day in DAY_NAMES if day in low],
        "starts": times.group(1).strip() if times else "",
        "ends": times.group(2).strip() if times else "",
        "text": " ".join((text or "").split())[:160],
    }


def _to_minutes(clock: str) -> int | None:
    match = re.match(r"(\d{1,2}):(\d{2})\s*([AP])M", (clock or "").strip(), re.I)
    if not match:
        return None
    hour, minute, meridiem = int(match.group(1)), int(match.group(2)), match.group(3).upper()
    hour = 0 if hour == 12 else hour
    if meridiem == "P":
        hour += 12
    return hour * 60 + minute


def is_overnight(card: dict) -> bool:
    """Does the shift run past midnight? 9:30 PM - 4:00 AM does."""
    start, end = _to_minutes(card.get("starts", "")), _to_minutes(card.get("ends", ""))
    if start is None or end is None:
        return False
    return end <= start


def card_is_acceptable(card: dict, prefs: dict | None) -> tuple[bool, str]:
    """Filter one schedule against the preferences. Returns (ok, reason)."""
    prefs = prefs or {}

    minimum = prefs.get("min_hours_per_week")
    if minimum is not None and card.get("hours_per_week") is not None:
        if card["hours_per_week"] < float(minimum):
            return False, f"{card['hours_per_week']}h/week below the {minimum}h minimum"

    available = [d.lower()[:3] for d in (prefs.get("available_days") or [])]
    if available and card.get("days"):
        # Every day of the shift has to be one you are free — a schedule you
        # can only half-work is one you cannot take.
        clashes = [day for day in card["days"] if day not in available]
        if clashes:
            return False, f"needs {', '.join(clashes)}, which you are not available"

    if prefs.get("avoid_overnight") and is_overnight(card):
        return False, f"overnight ({card.get('starts')} - {card.get('ends')})"

    return True, "acceptable"


def choose_card(cards: list[dict], prefs: dict | None = None) -> tuple[int | None, str]:
    """Index of the schedule to take, and why. None when none are acceptable.

    Among acceptable schedules: more pay first, then more hours. Both are
    tie-breaks that only run after the hard preferences have been applied.
    """
    acceptable: list[tuple[int, dict]] = []
    reasons: list[str] = []
    for index, card in enumerate(cards):
        ok, reason = card_is_acceptable(card, prefs)
        if ok:
            acceptable.append((index, card))
        else:
            reasons.append(f"#{index + 1} {reason}")

    if not acceptable:
        return None, "; ".join(reasons) or "no schedules on offer"

    acceptable.sort(
        key=lambda pair: (-(pair[1].get("pay_rate") or 0),
                          -(pair[1].get("hours_per_week") or 0),
                          pair[0])
    )
    index, card = acceptable[0]
    return index, (
        f"schedule #{index + 1} of {len(cards)}: "
        f"{card.get('starts') or '?'}-{card.get('ends') or '?'}, "
        f"{card.get('hours_per_week') or '?'}h/week, "
        f"{', '.join(card.get('days') or []) or 'days unknown'}"
    )

def rank_cards(cards: list[dict], prefs: dict | None = None) -> list[int]:
    """Every acceptable schedule, best first, as flyout indexes.

    choose_card answers "which one"; this answers "and then which". They are
    separate because the first choice frequently loses — a competing service
    takes the slot between the flyout rendering and the Apply landing — and the
    second schedule on the same job is a far cheaper thing to try than another
    job in another city.
    """
    acceptable: list[tuple[int, dict]] = []
    for index, card in enumerate(cards):
        ok, reason = card_is_acceptable(card, prefs)
        if ok:
            acceptable.append((index, card))
        else:
            log.debug("schedule #%d unacceptable: %s", index + 1, reason)

    acceptable.sort(
        key=lambda pair: (-(pair[1].get("pay_rate") or 0),
                          -(pair[1].get("hours_per_week") or 0),
                          pair[0])
    )
    return [index for index, _ in acceptable]


def describe_card(card: dict, index: int, total: int) -> str:
    return (
        f"schedule #{index + 1} of {total}: "
        f"{card.get('starts') or '?'}-{card.get('ends') or '?'}, "
        f"{card.get('hours_per_week') or '?'}h/week, "
        f"{', '.join(card.get('days') or []) or 'days unknown'}"
    )


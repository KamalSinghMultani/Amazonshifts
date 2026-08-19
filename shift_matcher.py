"""Shift model, stable identity, and filter matching.

Kept free of Playwright imports on purpose so it can be unit-tested without a
browser.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

log = logging.getLogger(__name__)


def _norm(value: Any) -> str:
    """Collapse whitespace and strip. None -> ''."""
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


# An hourly wage above this is not a wage. Seen live: Amazon returned
# 1.797...e308 — DBL_MAX, its "no pay data" sentinel — for a Nisku posting.
# Left alone that sorts ABOVE every genuine shift, so priority.order: pay
# would rank a posting with no pay data first in a batch.
IMPLAUSIBLE_PAY_RATE = 1000.0


def _to_float(value: Any) -> float | None:
    """Best-effort numeric parse. '$18.50/hr' -> 18.5, junk -> None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        match = re.search(r"\d+(?:\.\d+)?", str(value).replace(",", ""))
        if not match:
            return None
        number = float(match.group())

    # Treat a sentinel as missing rather than enormous: "unknown" already sorts
    # last, which is where a posting with no pay data belongs.
    if number != number or number in (float("inf"), float("-inf")):
        return None
    if abs(number) > IMPLAUSIBLE_PAY_RATE:
        log.debug("ignoring implausible pay rate %r", value)
        return None
    return number


@dataclass
class Shift:
    """One shift/job posting, normalized from either the DOM or the JSON API."""

    id: str | None = None
    title: str = ""
    location: str = ""
    schedule: str = ""
    pay_rate: float | None = None
    url: str | None = None
    raw: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.id = _norm(self.id) or None
        self.title = _norm(self.title)
        self.location = _norm(self.location)
        self.schedule = _norm(self.schedule)
        self.pay_rate = _to_float(self.pay_rate)
        self.url = _norm(self.url) or None

    @property
    def stable_id(self) -> str:
        """Dedup key.

        Prefers the site's own id. Falls back to a hash of the fields that
        identify a posting to a human, so a shift without an id still only
        alerts once.
        """
        if self.id:
            return f"id:{self.id}"
        basis = "|".join(
            (self.title.lower(), self.location.lower(), self.schedule.lower())
        )
        return "h:" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]

    def summary(self) -> str:
        bits = [self.title or "(untitled)"]
        if self.location:
            bits.append(self.location)
        if self.schedule:
            bits.append(self.schedule)
        if self.pay_rate is not None:
            bits.append(f"${self.pay_rate:.2f}/hr")
        return " — ".join(bits)



# Amazon's four building types, as its own onboarding presents them. Matching
# on the title is how you tell them apart: a "Delivery Station Warehouse
# Associate" and a "Fulfillment Center Warehouse Associate" are the same job in
# different buildings — neither needs a licence, and the distinction is about
# where you commute to, not what you do.
WAREHOUSE_TYPES: dict[str, tuple[str, ...]] = {
    "delivery station": ("delivery station", "delivery centre", "delivery center"),
    "fulfillment centre": ("fulfillment cent", "fulfilment cent"),
    "sortation centre": ("sortation", "sort centre", "sort center"),
    "xl warehouse": ("xl ",),
}


def warehouse_type(title: str) -> str:
    """Which of Amazon's building types a title belongs to, or ''."""
    low = (title or "").lower()
    for name, markers in WAREHOUSE_TYPES.items():
        if any(marker in low for marker in markers):
            return name
    return ""

def _contains_any(haystack: str, needles: Iterable[str]) -> str | None:
    """Return the first needle found in haystack, else None. Case-insensitive."""
    low = haystack.lower()
    for needle in needles:
        needle = _norm(needle).lower()
        if needle and needle in low:
            return needle
    return None


def _contains_any_word(haystack: str, needles: Iterable[str]) -> str | None:
    """Like _contains_any, but only on whole words.

    Place names demand this. "milton" is inside "Hamilton", so a plain
    substring match on a Milton filter accepted a Hamilton posting — 70km away
    and not somewhere you can work. The same trap caught "maple" matching
    "Maple Ridge, BC" earlier, which the province excludes were papering over.

    Titles deliberately still use substrings: "fulfillment cent" has to match
    both "Fulfillment Center" and "Fulfillment Centre".
    """
    low = haystack.lower()
    for needle in needles:
        needle = _norm(needle).lower()
        if not needle:
            continue
        # Guard only the ends that are letters. The province excludes are
        # written as ", bc" — a leading guard there would demand a non-letter
        # before the comma, which is never true, and would silently disable
        # every one of them.
        prefix = r"(?<![a-z])" if needle[0].isalpha() else ""
        suffix = r"(?![a-z])" if needle[-1].isalpha() else ""
        if re.search(prefix + re.escape(needle) + suffix, low):
            return needle
    return None



def _parse_when(value: Any):
    """An ISO timestamp, or None. Anything unparseable is fatal at startup.

    A test window that silently never opens looks exactly like a quiet night,
    and one that silently never CLOSES holds shifts you cannot work.
    """
    if not value:
        return None
    from datetime import datetime

    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).strip())
    except ValueError:
        raise ValueError(
            f"filters.accept_everything_until must be an ISO timestamp like "
            f"2026-08-19T02:00, got {value!r}"
        ) from None


class ShiftMatcher:
    """Decides whether a Shift is one the user actually wants."""

    def __init__(self, filters: dict | None = None) -> None:
        filters = filters or {}
        self.include_titles = list(filters.get("include_titles") or [])
        self.exclude_titles = list(filters.get("exclude_titles") or [])
        self.include_locations = list(filters.get("include_locations") or [])
        self.exclude_locations = list(filters.get("exclude_locations") or [])
        self.include_schedules = list(filters.get("include_schedules") or [])
        self.exclude_schedules = list(filters.get("exclude_schedules") or [])
        self.min_pay_rate = _to_float(filters.get("min_pay_rate"))
        # Both empty means "any" — the same convention as the include_ lists,
        # and the same default Amazon's own onboarding starts from with every
        # box ticked.
        # A time-boxed "take anything" window, for proving the hold path when
        # nothing commutable is posted. It expires by ITSELF: a test mode you
        # have to remember to switch off is one that ends up holding a shift in
        # Nova Scotia three weeks later.
        self.accept_everything_until = _parse_when(filters.get("accept_everything_until"))
        self.warehouse_types = [
            str(t).strip().lower() for t in (filters.get("warehouse_types") or [])
        ]
        self.shift_types = [
            str(t).strip().lower() for t in (filters.get("shift_types") or [])
        ]

    def test_window_open(self, now: Any = None) -> bool:
        if self.accept_everything_until is None:
            return False
        from datetime import datetime as _dt

        return (now or _dt.now()) < self.accept_everything_until

    def matches(self, shift: Shift) -> tuple[bool, str]:
        """Return (matched, reason). The reason is logged, which makes the
        dry-run period actually diagnosable."""
        if self.test_window_open():
            return True, (
                "TEST WINDOW — filters bypassed until "
                f"{self.accept_everything_until:%H:%M}"
            )

        checks = (
            ("title", shift.title, self.include_titles, self.exclude_titles),
            ("location", shift.location, self.include_locations, self.exclude_locations),
            ("schedule", shift.schedule, self.include_schedules, self.exclude_schedules),
        )

        for name, value, includes, excludes in checks:
            # Place names match on whole words; titles on substrings.
            match = _contains_any_word if name == "location" else _contains_any
            hit = match(value, excludes)
            if hit:
                return False, f"{name} excluded by {hit!r}"
            if includes and not match(value, includes):
                return False, f"{name} {value!r} matched no include filter"

        if self.warehouse_types:
            found = warehouse_type(shift.title)
            if not found:
                return False, f"could not tell the warehouse type of {shift.title!r}"
            if found not in self.warehouse_types:
                return False, f"{found} is not one of the warehouse types you want"

        if self.shift_types:
            # jobType comes through as PART_TIME / FULL_TIME / FLEX_TIME /
            # REDUCED_TIME, sometimes several joined together.
            haystack = f"{shift.schedule} {shift.title}".lower().replace("_", " ")
            if not any(wanted.replace("_", " ") in haystack for wanted in self.shift_types):
                return False, f"shift type {shift.schedule!r} is not one you want"

        if self.min_pay_rate is not None:
            if shift.pay_rate is None:
                return False, "min_pay_rate set but shift has no pay rate"
            if shift.pay_rate < self.min_pay_rate:
                return False, f"pay {shift.pay_rate} < min {self.min_pay_rate}"

        return True, "matched all filters"

    def filter(self, shifts: Iterable[Shift]) -> list[Shift]:
        return [s for s in shifts if self.matches(s)[0]]


def _rank_in(value: str, preferences: list[str]) -> int:
    """Position of the first preference found in value; unlisted sorts last.

    Earlier in the list means more wanted, so this is directly a sort key.
    """
    low = (value or "").lower()
    for index, preference in enumerate(preferences):
        preference = _norm(preference).lower()
        if preference and preference in low:
            return index
    return len(preferences)


class ShiftRanker:
    """Decides which matching shift you want *most*.

    Filtering answers "is this acceptable"; ranking answers "which one first".
    They are separate because a whole batch can land in one poll, and only the
    front of the ranked list gets alerted individually and held — so this is
    what actually decides which shift the watcher goes after.

    `order` names the tie-break sequence, e.g. ["title", "location", "pay"]:
    role decides first, closer site breaks ties, pay breaks what is left.
    """

    CRITERIA = ("title", "location", "pay")

    def __init__(self, priority: dict | None = None) -> None:
        priority = priority or {}
        self.titles = [t for t in (priority.get("titles") or []) if _norm(t)]
        self.locations = [l for l in (priority.get("locations") or []) if _norm(l)]
        self.demote_titles = [t for t in (priority.get("demote_titles") or []) if _norm(t)]
        order = [str(o).strip().lower() for o in (priority.get("order") or [])]
        self.order = [o for o in order if o in self.CRITERIA] or list(self.CRITERIA)

    def key(self, shift: Shift) -> tuple:
        parts = []
        for criterion in self.order:
            if criterion == "title":
                # A demoted role loses to every non-demoted one regardless of
                # how well it scores otherwise — that is what "last" means.
                demoted = 1 if _contains_any(shift.title, self.demote_titles) else 0
                parts.append((demoted, _rank_in(shift.title, self.titles)))
            elif criterion == "location":
                parts.append((_rank_in(shift.location, self.locations),))
            else:
                # Higher pay first; unknown pay sorts last rather than winning.
                parts.append((shift.pay_rate is None, -(shift.pay_rate or 0)))
        return tuple(parts)

    def sort(self, shifts: Iterable[Shift]) -> list[Shift]:
        return sorted(shifts, key=self.key)

    def explain(self, shift: Shift) -> str:
        """Why this shift ranks where it does — for the logs."""
        bits = []
        if _contains_any(shift.title, self.demote_titles):
            bits.append("demoted role")
        title_rank = _rank_in(shift.title, self.titles)
        if title_rank < len(self.titles):
            bits.append(f"title #{title_rank + 1}")
        location_rank = _rank_in(shift.location, self.locations)
        if location_rank < len(self.locations):
            bits.append(f"location #{location_rank + 1}")
        return ", ".join(bits) or "no priority rule matched"

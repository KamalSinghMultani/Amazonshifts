"""Shift model, stable identity, and filter matching.

Kept free of Playwright imports on purpose so it can be unit-tested without a
browser.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Iterable


def _norm(value: Any) -> str:
    """Collapse whitespace and strip. None -> ''."""
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def _to_float(value: Any) -> float | None:
    """Best-effort numeric parse. '$18.50/hr' -> 18.5, junk -> None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"\d+(?:\.\d+)?", str(value).replace(",", ""))
    return float(match.group()) if match else None


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


def _contains_any(haystack: str, needles: Iterable[str]) -> str | None:
    """Return the first needle found in haystack, else None. Case-insensitive."""
    low = haystack.lower()
    for needle in needles:
        needle = _norm(needle).lower()
        if needle and needle in low:
            return needle
    return None


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

    def matches(self, shift: Shift) -> tuple[bool, str]:
        """Return (matched, reason). The reason is logged, which makes the
        dry-run period actually diagnosable."""
        checks = (
            ("title", shift.title, self.include_titles, self.exclude_titles),
            ("location", shift.location, self.include_locations, self.exclude_locations),
            ("schedule", shift.schedule, self.include_schedules, self.exclude_schedules),
        )

        for name, value, includes, excludes in checks:
            hit = _contains_any(value, excludes)
            if hit:
                return False, f"{name} excluded by {hit!r}"
            if includes and not _contains_any(value, includes):
                return False, f"{name} {value!r} matched no include filter"

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

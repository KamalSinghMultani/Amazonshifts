"""Structured timing records for one real hold attempt.

Each attempt is appended to logs/hold_timings.jsonl.  The file contains timing,
status, and non-secret identifiers only; no cookies, auth headers, request
bodies, candidate ids, or tokens are recorded.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Iterable


DEFAULT_PATH = Path("logs/hold_timings.jsonl")


def _safe(value):
    if value is None:
        return None
    text = str(value)
    return text[:300]


def append_record(
    *,
    job_id: str | None,
    schedule_id: str | None,
    title: str,
    location: str,
    status: str,
    message: str,
    poll_to_dispatch_ms: float | None,
    total_from_poll_ms: float | None,
    hold_timings: Iterable[tuple[str, float]] = (),
    backend_detail: str = "",
    path: Path = DEFAULT_PATH,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "recorded_at": datetime.now().isoformat(timespec="milliseconds"),
        "job_id": _safe(job_id),
        "schedule_id": _safe(schedule_id),
        "title": _safe(title),
        "location": _safe(location),
        "status": _safe(status),
        "message": _safe(message),
        "poll_to_dispatch_ms": None if poll_to_dispatch_ms is None else round(float(poll_to_dispatch_ms), 2),
        "total_from_poll_ms": None if total_from_poll_ms is None else round(float(total_from_poll_ms), 2),
        "stages": [
            {"name": _safe(label), "ms_from_hold_start": round(float(ms), 2)}
            for label, ms in (hold_timings or [])
        ],
        "backend_detail": _safe(backend_detail),
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _lines(path: Path = DEFAULT_PATH) -> list[str]:
    if not path.exists():
        return []
    return [line for line in path.read_text("utf-8").splitlines() if line.strip()]


def count(path: Path = DEFAULT_PATH) -> int:
    return len(_lines(path))


def latest(path: Path = DEFAULT_PATH) -> dict | None:
    lines = _lines(path)
    if not lines:
        return None
    return json.loads(lines[-1])


def latest_after(start_count: int, path: Path = DEFAULT_PATH) -> dict | None:
    """Latest record created after a caller's starting line count."""
    lines = _lines(path)
    if len(lines) <= max(0, int(start_count)):
        return None
    return json.loads(lines[-1])

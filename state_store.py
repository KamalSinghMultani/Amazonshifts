"""Persisted 'already seen' set, so a shift only ever alerts once.

Entries expire after ttl_hours so a genuinely re-posted shift can alert again.
Writes are atomic (tmp file + os.replace) — a crash mid-write cannot leave a
truncated state file that would cause a re-alert storm on restart.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path

log = logging.getLogger(__name__)


class StateStore:
    def __init__(
        self,
        path: str | os.PathLike,
        ttl_hours: float = 72,
        detections_path: str | os.PathLike | None = None,
    ) -> None:
        self.path = Path(path)
        self.ttl_seconds = float(ttl_hours) * 3600 if ttl_hours else None
        self.detections_path = Path(detections_path) if detections_path else None
        self._seen: dict[str, dict] = {}
        self._load()

    # ── persistence ─────────────────────────────────────────────────────────
    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text("utf-8"))
            entries = data.get("seen", {})
            if isinstance(entries, dict):
                self._seen = {k: v for k, v in entries.items() if isinstance(v, dict)}
        except (json.JSONDecodeError, OSError) as exc:
            # A corrupt state file must not stop the watcher. Worst case we
            # re-alert on shifts we already saw.
            log.warning("could not read state file %s (%s) — starting empty", self.path, exc)
            self._seen = {}
        self.prune()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"version": 1, "seen": self._seen}, indent=2)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.replace(tmp, self.path)
        except OSError as exc:
            log.error("could not write state file %s: %s", self.path, exc)
            if os.path.exists(tmp):
                os.unlink(tmp)

    # ── api ─────────────────────────────────────────────────────────────────
    def has_seen(self, key: str) -> bool:
        entry = self._seen.get(key)
        if entry is None:
            return False
        if self._expired(entry):
            del self._seen[key]
            return False
        return True

    def mark_seen(self, key: str, note: str = "") -> None:
        self._seen[key] = {"ts": time.time(), "note": note}

    def prune(self) -> int:
        """Drop expired entries. Returns how many were removed."""
        if self.ttl_seconds is None:
            return 0
        stale = [k for k, v in self._seen.items() if self._expired(v)]
        for key in stale:
            del self._seen[key]
        return len(stale)

    # ── detection log ───────────────────────────────────────────────────────
    def log_detection(self, key: str, summary: str = "") -> None:
        """Append one detection, for --drop-report to read back later.

        Best-effort by design: analytics must never be able to break detection,
        so every failure here is a log line and nothing more.
        """
        if not self.detections_path:
            return
        try:
            self.detections_path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps({"ts": time.time(), "id": key, "summary": summary})
            with self.detections_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except (OSError, TypeError, ValueError) as exc:
            log.warning("could not log detection: %s", exc)

    def read_detections(self) -> list[dict]:
        """Every logged detection. Torn or truncated lines are skipped, not
        fatal — the process can be killed mid-append at any time."""
        if not self.detections_path or not self.detections_path.exists():
            return []
        entries: list[dict] = []
        try:
            text = self.detections_path.read_text("utf-8")
        except OSError as exc:
            log.warning("could not read %s: %s", self.detections_path, exc)
            return []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue  # a torn last line from a kill mid-write
            if isinstance(entry, dict) and isinstance(entry.get("ts"), (int, float)):
                entries.append(entry)
        return entries

    def _expired(self, entry: dict) -> bool:
        if self.ttl_seconds is None:
            return False
        return (time.time() - float(entry.get("ts", 0))) > self.ttl_seconds

    def __len__(self) -> int:
        return len(self._seen)

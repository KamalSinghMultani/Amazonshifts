"""Fast path: read shifts straight from the site's own JSON endpoint.

Why this exists: reloading and scraping a React page costs seconds. Hitting the
endpoint the page itself calls costs milliseconds, and against bots that snipe
shifts in under a second, that gap is the whole game.

The request goes through Playwright's `context.request`, which shares the
browser context's cookies — so the session saved by save_session.py is reused
with no extra work.

`dig` and `parse_shifts` are pure functions and unit-tested without a browser.
"""

from __future__ import annotations

import logging
from typing import Any

from shift_matcher import Shift

log = logging.getLogger(__name__)


def dig(payload: Any, path: str) -> Any:
    """Walk a dotted path into nested dicts/lists.

    "data.jobCards"    -> payload["data"]["jobCards"]
    "results.0.cards"  -> payload["results"][0]["cards"]

    Returns None if any step is missing, rather than raising — a schema change
    on Amazon's side should degrade to 'found nothing', not crash the watcher.
    """
    if not path:
        return payload
    current = payload
    for part in path.split("."):
        if current is None:
            return None
        if isinstance(current, list):
            if not part.isdigit() or int(part) >= len(current):
                return None
            current = current[int(part)]
        elif isinstance(current, dict):
            if part not in current:
                return None
            current = current[part]
        else:
            return None
    return current


def parse_shifts(
    payload: Any,
    shifts_path: str,
    field_map: dict[str, str | None],
    url_template: str | None = None,
) -> list[Shift]:
    """Turn a raw JSON response into Shift objects."""
    items = dig(payload, shifts_path)
    if items is None:
        log.warning("shifts_path %r found nothing in the response", shifts_path)
        return []
    if not isinstance(items, list):
        log.warning("shifts_path %r pointed at %s, expected a list", shifts_path, type(items).__name__)
        return []

    shifts: list[Shift] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        values = {
            field: (dig(item, source) if source else None)
            for field, source in field_map.items()
            if field in {"id", "title", "location", "schedule", "pay_rate", "url"}
        }
        shift = Shift(**values, raw=item)
        if not shift.url and url_template and shift.id:
            shift.url = url_template.format(id=shift.id)
        shifts.append(shift)
    return shifts


class ApiClient:
    """Polls the JSON endpoint using the browser context's cookies."""

    def __init__(self, request_context, api_cfg: dict, timeout_ms: int = 15000) -> None:
        self.request = request_context
        self.endpoint_url = api_cfg["endpoint_url"]
        self.method = str(api_cfg.get("method", "POST")).upper()
        self.payload = api_cfg.get("payload")
        self.shifts_path = api_cfg.get("shifts_path", "")
        self.field_map = api_cfg.get("field_map") or {}
        self.url_template = api_cfg.get("url_template")
        self.extra_headers = api_cfg.get("extra_headers") or {}
        self.timeout_ms = timeout_ms

    def fetch_shifts(self) -> list[Shift]:
        """One poll. Raises on transport/HTTP failure so the watcher's circuit
        breaker can count it."""
        headers = {"accept": "application/json", **self.extra_headers}

        if self.method == "GET":
            response = self.request.get(
                self.endpoint_url,
                params=self.payload or {},
                headers=headers,
                timeout=self.timeout_ms,
            )
        else:
            response = self.request.post(
                self.endpoint_url,
                data=self.payload or {},
                headers={"content-type": "application/json", **headers},
                timeout=self.timeout_ms,
            )

        if not response.ok:
            body = ""
            try:
                body = response.text()[:300]
            except Exception:  # noqa: BLE001 - body is diagnostics only
                pass
            raise RuntimeError(f"API returned {response.status}: {body}")

        try:
            payload = response.json()
        except Exception as exc:  # noqa: BLE001 - playwright raises a bare Error
            raise RuntimeError(f"API response was not JSON: {exc}") from exc

        return parse_shifts(payload, self.shifts_path, self.field_map, self.url_template)

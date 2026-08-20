"""Observe the browser-driven application flow and verify the backend reserve.

This does not replay candidate-application calls. It only listens to the same
responses Amazon's own frontend receives after Create Application is clicked.
"""

from __future__ import annotations

import logging
import time
from typing import Any

log = logging.getLogger(__name__)


class SoftReserveObserver:
    def __init__(self, page: Any, expected_schedule_id: str | None = None) -> None:
        self.page = page
        self.expected_schedule_id = expected_schedule_id or ""
        self.confirmed = False
        self.expiration = None
        self.schedule_id = None
        self.application_id = None
        self.relevant_update_seen = False
        self.last_update_at: float | None = None
        self.last_state = None
        self.last_schedule_matched = False
        self._handler = None

    def __enter__(self):
        def on_response(response):
            try:
                if "candidate-application/update-application" not in (response.url or ""):
                    return
                payload = response.json()
                data = payload.get("data") if isinstance(payload, dict) else None
                if not isinstance(data, dict):
                    return
                selected = data.get("jobScheduleSelected") or {}
                schedule_id = selected.get("scheduleId") if isinstance(selected, dict) else None
                expiration = data.get("softReserveExpirationTimestamp")
                state = data.get("currentState")
                self.application_id = data.get("applicationId") or self.application_id
                self.schedule_id = schedule_id or self.schedule_id
                self.expiration = expiration or self.expiration

                schedule_matches = (
                    not self.expected_schedule_id
                    or schedule_id == self.expected_schedule_id
                )
                self.last_state = state
                self.last_schedule_matched = bool(schedule_id and schedule_matches)
                if self.last_schedule_matched:
                    self.relevant_update_seen = True
                    self.last_update_at = time.monotonic()
                if schedule_matches and expiration and state == "JOB_SELECTED":
                    self.confirmed = True
                    log.info(
                        "backend soft reserve confirmed: schedule=%s expires=%s",
                        schedule_id, expiration,
                    )
            except Exception as exc:  # noqa: BLE001 - observation must never break a hold
                log.debug("could not inspect update-application response: %s", exc)

        self._handler = on_response
        self.page.on("response", on_response)
        return self

    def __exit__(self, *_exc):
        try:
            if self._handler is not None:
                self.page.remove_listener("response", self._handler)
        except Exception:
            pass

    def settled_without_confirmation(self, *, quiet_ms: int = 2500) -> bool:
        """Whether a relevant passive update settled without reserve proof.

        This is only an early UNCERTAIN signal.  It can never confirm or deny a
        reserve.  A JOB_SELECTED response missing only its expiration is given
        the full confirmation window because a follow-up response may complete
        the proof.
        """
        if (
            self.confirmed
            or not self.relevant_update_seen
            or self.last_update_at is None
            or self.last_state == "JOB_SELECTED"
        ):
            return False
        return (time.monotonic() - self.last_update_at) * 1000 >= max(0, quiet_ms)

    def detail(self) -> str:
        if not self.confirmed:
            return ""
        bits = ["backend soft reserve confirmed"]
        if self.schedule_id:
            bits.append(f"schedule {self.schedule_id}")
        if self.expiration:
            bits.append(f"expires {self.expiration}")
        return " — ".join(bits)

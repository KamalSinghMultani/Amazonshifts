"""Final session bootstrap layer for the optimized watcher.

v3 already keeps detection running while a child process checks/refreshes the
Amazon Hiring session. v4 makes startup deterministic without throwing away a
known-good browser session: it first asks the helper for STRONG country/session
proof, and that helper only performs a fresh login when the proof fails. A
verified child state is then imported as cookies plus country-site localStorage.
"""

from __future__ import annotations

import json
import logging
import time
from urllib.parse import urlparse

import watcher as base
import watcher_v3

log = logging.getLogger("watcher")


def _origin(url: str) -> str:
    parsed = urlparse(url or "")
    if not (parsed.scheme and parsed.hostname):
        return ""
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{parsed.hostname}{port}".lower()


class AutoSessionWatcher(watcher_v3.OptimizedWatcher):
    """Optimized watcher with immediate, verified session bootstrap/recovery."""

    def _apply_refreshed_state(self) -> None:
        """Import the verified child-session state into the live context.

        v3 imported cookies only. Playwright storage_state also carries origin
        localStorage, including frontend session/token state. Copy the entries
        for the configured Hiring country origin before reminting the API token.
        """
        payload = json.loads(self.refresh_state_path.read_text("utf-8"))
        cookies = payload.get("cookies") if isinstance(payload, dict) else None
        cookies = cookies or []
        if cookies:
            self.context.add_cookies(cookies)

        base_url = self.cfg["site"]["base_url"]
        expected_origin = _origin(base_url)
        local_entries = []
        if isinstance(payload, dict):
            for item in payload.get("origins") or []:
                if _origin(str(item.get("origin") or "")) == expected_origin:
                    local_entries.extend(item.get("localStorage") or [])

        if self.page is not None:
            try:
                if _origin(self.page.url) != expected_origin:
                    self.page.goto(
                        self.cfg["site"]["job_search_url"],
                        wait_until="domcontentloaded",
                    )
                if local_entries:
                    self.page.evaluate(
                        """entries => {
                            for (const item of entries) {
                                if (item && typeof item.name === 'string') {
                                    localStorage.setItem(item.name, item.value ?? '');
                                }
                            }
                        }""",
                        local_entries,
                    )
                if self.token_source is not None:
                    self.token_source.refresh()
            except Exception as exc:  # noqa: BLE001
                log.warning("verified session imported but live-page refresh failed: %s", exc)

        try:
            self.context.storage_state(path=self.cfg["browser"]["storage_state"])
        except Exception as exc:  # noqa: BLE001
            log.debug("could not persist imported verified session state: %s", exc)

        log.info(
            "verified session state imported into live watcher: %d cookie(s), "
            "%d localStorage item(s) for %s",
            len(cookies), len(local_entries), expected_origin,
        )

    def _loop(self, once: bool = False) -> None:
        # A long-running live watcher must not mistake public API access for an
        # authenticated application session, but it also should not force a new
        # login every launch. The child first runs the strict existing-session
        # proof. If that fails, session_refresh runs the project's existing auth
        # state machine and verifies the recovered session before returning it.
        #
        # The helper runs in a child process, so GraphQL detection continues.
        # --once stays a pure one-poll diagnostic and dry-run never logs in.
        if not once and self.auto_relogin and not self.dry_run:
            started = self._start_session_worker(
                force_login=False,
                reason="startup strong session proof/recovery",
            )
            if started:
                now = time.monotonic()
                if self.session_check_every:
                    self.next_session_check = now + self.session_check_every
                log.info(
                    "strong Canadian session proof/recovery started in background; "
                    "detection continues"
                )

        super()._loop(once=once)


# Reuse watcher.py's CLI/config/doctor plumbing with the final class.
base.Watcher = AutoSessionWatcher


if __name__ == "__main__":
    raise SystemExit(base.main())

"""Final session bootstrap layer for the optimized watcher.

v3 already keeps detection running while a child process checks/refreshes the
Amazon Hiring session. v4 makes startup deterministic: a normal live run starts
a fresh country-specific login in the background immediately, then imports the
verified cookies AND country-site localStorage into the live watcher before
resuming the normal periodic health/proactive-refresh cadence.
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
    """Optimized watcher with immediate, verified session bootstrap."""

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
        # A long-running live watcher should begin with a FRESH login rather
        # than trusting an old application shell. The child process does the
        # slow auth work, so GraphQL detection continues in this process.
        #
        # --once intentionally remains a pure one-poll diagnostic, and dry-run
        # never performs login clicks.
        if not once and self.auto_relogin and not self.dry_run:
            started = self._start_session_worker(
                force_login=True,
                reason="startup fresh Canadian session proof",
            )
            if started:
                # Do not immediately launch the ordinary 5-minute health check
                # when this worker returns. Start that cadence from now instead.
                now = time.monotonic()
                if self.session_check_every:
                    self.next_session_check = now + self.session_check_every
                log.info(
                    "fresh Canadian login/proof started in background; detection continues"
                )

        super()._loop(once=once)


# Reuse watcher.py's CLI/config/doctor plumbing with the final class.
base.Watcher = AutoSessionWatcher


if __name__ == "__main__":
    raise SystemExit(base.main())

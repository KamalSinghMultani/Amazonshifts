"""Final session bootstrap layer for the optimized watcher.

v3 already keeps detection running while a child process checks/refreshes the
Amazon Hiring session. v4 makes startup deterministic: a normal live run starts
a fresh country-specific login in the background immediately, then resumes the
normal periodic health/proactive-refresh cadence after that bootstrap.
"""

from __future__ import annotations

import logging
import time

import watcher as base
import watcher_v3

log = logging.getLogger("watcher")


class AutoSessionWatcher(watcher_v3.OptimizedWatcher):
    """Optimized watcher with immediate, verified session bootstrap."""

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

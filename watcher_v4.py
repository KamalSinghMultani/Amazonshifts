"""Final session bootstrap layer for the optimized watcher.

v3 already keeps detection running while a child process checks/refreshes the
Amazon Hiring session. One startup gap remained: the first health check was
scheduled several minutes in the future. If the watcher launched with an
expired portal session it could therefore detect shifts before it had even
started repairing the login.

v4 starts that existing background session worker immediately on a normal live
run. It does not block detection: polling begins while the helper checks the
session and, when necessary, performs the configured login flow. Normal
periodic health checks and proactive refreshes remain owned by watcher_v3.
"""

from __future__ import annotations

import logging

import watcher as base
import watcher_v3

log = logging.getLogger("watcher")


class AutoSessionWatcher(watcher_v3.OptimizedWatcher):
    """Optimized watcher with immediate non-blocking session bootstrap."""

    def __init__(self, cfg: dict, live_override: bool = False) -> None:
        super().__init__(cfg, live_override=live_override)

        # A live watcher with auto_relogin enabled should not wait five minutes
        # before discovering that its application session is dead. Mark the
        # health check due now. The helper still runs in a separate process, so
        # this never stalls GraphQL detection.
        if self.auto_relogin and not self.dry_run:
            self.next_session_check = 0.0

    def _loop(self, once: bool = False) -> None:
        # For the long-running watcher, start session maintenance before the
        # first poll. _start_session_worker() returns immediately; the child
        # process does the potentially slow page/login/OTP work while this
        # process continues into the detector loop.
        #
        # --once intentionally remains a pure one-poll diagnostic and does not
        # leave a background login helper running after the parent exits.
        if not once and self.auto_relogin and not self.dry_run:
            started = self._start_session_worker(
                force_login=False,
                reason="startup session bootstrap",
            )
            if started:
                log.info(
                    "automatic session bootstrap started; detection continues while login is checked"
                )

        super()._loop(once=once)


# Reuse watcher.py's CLI/config/doctor plumbing with the final class.
base.Watcher = AutoSessionWatcher


if __name__ == "__main__":
    raise SystemExit(base.main())

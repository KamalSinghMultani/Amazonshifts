"""Keep a live `authorization` token for the JSON endpoint.

The problem this solves: hiring.amazon.* returns 401 for the graphql endpoint
without an `authorization` header (measured), the token is minted by the page's
own JavaScript, and it rotates. A token pasted into config.yaml works for a
while and then silently starts 401ing — which looks exactly like "no shifts
today", the worst failure this project has.

So we never store one. A page stays open on the app; every request it makes
carries a current token, and we keep the last one we saw. Reloading that page
mints a fresh one on demand.

Two sources, in order:
  1. the newest `authorization` header seen on a request from the live page
  2. a localStorage key, if one is configured and holds the same value

Source 1 needs no knowledge of the site's internals, which is why it leads:
whatever the app does to build its token, it has to put the result on the wire.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


class TokenSource:
    def __init__(
        self,
        page: Any,
        *,
        endpoint_url: str,
        header: str = "authorization",
        storage_key: str | None = None,
        reload_url: str | None = None,
        settle_ms: int = 6000,
    ) -> None:
        self.page = page
        self.endpoint_host = (endpoint_url or "").split("?")[0]
        self.header = header.lower()
        self.storage_key = storage_key
        self.reload_url = reload_url
        self.settle_ms = settle_ms
        self._token: str | None = None

        page.on("request", self._on_request)

    # ── harvesting ──────────────────────────────────────────────────────────
    def _on_request(self, request: Any) -> None:
        """Remember the token off any request the page makes to the endpoint.

        Wrapped whole: this runs on Playwright's event thread, and an exception
        here must not be able to disturb polling.
        """
        try:
            if not request.url.startswith(self.endpoint_host):
                return
            value = request.all_headers().get(self.header)
            if value and value != self._token:
                self._token = value
                log.debug("captured a fresh %s token (%d chars)", self.header, len(value))
        except Exception as exc:  # noqa: BLE001
            log.debug("could not read token off a request: %s", exc)

    def _from_storage(self) -> str | None:
        if not self.storage_key:
            return None
        try:
            return self.page.evaluate(
                "key => localStorage.getItem(key)", self.storage_key
            )
        except Exception as exc:  # noqa: BLE001 - page may be navigating
            log.debug("could not read localStorage[%s]: %s", self.storage_key, exc)
            return None

    # ── api ─────────────────────────────────────────────────────────────────
    def current(self) -> str | None:
        """The freshest token we know about. Cheap — safe to call every poll."""
        stored = self._from_storage()
        if stored:
            # localStorage is authoritative when present: the page updates it on
            # rotation, whereas our captured header is only as new as the last
            # request the page happened to make.
            if stored != self._token:
                log.debug("token refreshed from localStorage")
                self._token = stored
            return stored
        return self._token

    def refresh(self) -> str | None:
        """Force a new token by reloading the page that mints them.

        Called after a 401. Costs a page load, but only when the token has
        actually expired — not on every poll.
        """
        try:
            if self.reload_url:
                self.page.goto(self.reload_url, wait_until="domcontentloaded")
            else:
                self.page.reload(wait_until="domcontentloaded")
            self.page.wait_for_timeout(self.settle_ms)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not reload the page to refresh the token: %s", exc)
        return self.current()

"""Record the HTTP a hold actually makes, so it can one day be replayed.

WHY
---
Holding currently drives a real browser: click a card, wait for Amazon's page
to load, click again. Measured, about seven of those nine seconds are page
loads and React rendering — none of it ours. Underneath, every click is just
an HTTPS request, and "Create Application" is presumably a single POST.

Detection already went through this transition: scraping the rendered page
took 5,700ms and saw 25 jobs; calling searchJobCardsByLocation directly takes
150ms and sees 99. The hold could make the same jump — but only if we know the
exact request, and navigating straight to the application URL was refused six
different ways, so the state must be established by something we have not
captured yet.

So: watch a real hold and write down what it sends. Costs nothing until a hold
happens, and turns "maybe a 2-second hold is possible" into a question with an
answer.

WHAT IS WRITTEN
---------------
Method, URL, request headers, request body, response status, and a slice of
the response — for the requests a hold makes, filtered to Amazon's own hosts.

Cookies and authorization headers are REDACTED. They are the session itself,
the file lands on disk, and a captured recipe is worth nothing if publishing it
hands someone your account.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

OUT_DIR = Path("api_captures")

SENSITIVE = {"cookie", "authorization", "x-api-key", "anti-csrftoken-a2z",
             "x-amz-security-token", "set-cookie"}

# Only Amazon's own traffic. Analytics, fonts and CDNs are noise.
INTERESTING_HOSTS = ("hiring.amazon.", "auth.hiring.amazon.")

# The parts of the flow worth replaying.
INTERESTING_PATHS = ("/application", "/graphql", "/authorize", "/api/")


def _wanted(url: str) -> bool:
    return (any(host in url for host in INTERESTING_HOSTS)
            and any(path in url for path in INTERESTING_PATHS))


def _redacted(headers: dict) -> dict:
    return {
        key: ("<redacted>" if key.lower() in SENSITIVE else value)
        for key, value in (headers or {}).items()
    }


class HoldRecorder:
    """Attach around a hold; detach after. Never raises."""

    def __init__(self, context: Any, label: str = "hold") -> None:
        self.context = context
        self.label = label
        self.calls: list[dict] = []
        self._handler = None
        self.started = time.time()

    def __enter__(self) -> "HoldRecorder":
        def on_response(response):
            try:
                request = response.request
                if not _wanted(response.url):
                    return
                body = None
                try:
                    body = request.post_data
                except Exception:  # noqa: BLE001
                    pass
                snippet = ""
                try:
                    snippet = response.text()[:600]
                except Exception:  # noqa: BLE001 - binary or already consumed
                    pass
                self.calls.append({
                    "at": round(time.time() - self.started, 3),
                    "method": request.method,
                    "url": response.url,
                    "status": response.status,
                    "resource_type": request.resource_type,
                    "request_headers": _redacted(request.all_headers()),
                    "request_body": (body or "")[:4000] or None,
                    "response": snippet,
                })
            except Exception as exc:  # noqa: BLE001 - recording is never fatal
                log.debug("could not record a request: %s", exc)

        self._handler = on_response
        try:
            self.context.on("response", on_response)
        except Exception as exc:  # noqa: BLE001
            log.debug("could not attach the recorder: %s", exc)
        return self

    def __exit__(self, *_exc) -> None:
        try:
            if self._handler is not None:
                self.context.remove_listener("response", self._handler)
        except Exception as exc:  # noqa: BLE001
            log.debug("could not detach the recorder: %s", exc)
        self.save()

    def save(self) -> Path | None:
        if not self.calls:
            return None
        try:
            OUT_DIR.mkdir(exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            path = OUT_DIR / f"{self.label}-{stamp}.json"
            path.write_text(json.dumps(self.calls, indent=2), "utf-8")
            log.info("recorded %d request(s) from the hold -> %s", len(self.calls), path)
            return path
        except Exception as exc:  # noqa: BLE001
            log.warning("could not write the hold recording: %s", exc)
            return None

    def summary(self) -> list[str]:
        """One line per call, for the log."""
        return [
            f"  +{call['at']:>6.2f}s {call['method']:<5} {call['status']} "
            f"{call['url'][:96]}"
            for call in self.calls
        ]

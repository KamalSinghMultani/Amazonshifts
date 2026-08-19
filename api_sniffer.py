"""Discover the JSON endpoint the site uses to list shifts.

Run this, then browse hiring.amazon.ca normally in the window that opens
(search for jobs, change filters). Every JSON XHR/fetch response is written to
api_captures/ along with the request method, URL, headers, and body.

    python api_sniffer.py

Then look through api_captures/index.md for the request that carries the job
list, and copy its details into the `api:` block of config.yaml.

⚠️  If the winning request needs an `authorization: Bearer …` header, be aware
    that tokens expire and rotate — unlike cookies, which Playwright refreshes
    for you. A pasted token will work for a while and then silently start
    returning 401. The sniffer flags any request where it sees one.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

import browser_launch
from config import load_config

OUT_DIR = Path("api_captures")
SENSITIVE_HEADERS = {"authorization", "x-api-key", "cookie", "x-amz-security-token"}
# Endpoints that are obviously not job data — skip to keep the capture readable.
NOISE = re.compile(r"(analytics|telemetry|metrics|beacon|csm|log|rum)", re.I)


def _slug(url: str, index: int) -> str:
    tail = re.sub(r"[^a-zA-Z0-9]+", "-", url.split("?")[0][-60:]).strip("-")
    return f"{index:03d}-{tail or 'request'}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Discover the site's JSON endpoint")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument(
        "--seconds",
        type=float,
        default=None,
        help="capture for this long and exit, instead of waiting for Enter. "
             "The job list loads by itself, so this needs no browsing.",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="only useful with --seconds — there is nothing to watch",
    )
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    site = cfg["site"]
    browser_cfg = cfg["browser"]
    storage = Path(browser_cfg["storage_state"])

    OUT_DIR.mkdir(exist_ok=True)
    captures: list[dict] = []

    print(f"Captures will be written to {OUT_DIR}/")
    if args.seconds is None:
        print("Browse the site normally — search jobs, change filters.")
        print("Press Enter in this terminal when you are done.\n")
    else:
        print(f"Capturing for {args.seconds:.0f}s, no interaction needed.\n")

    with sync_playwright() as playwright:
        browser, context = browser_launch.launch_context(
            playwright,
            browser_cfg,
            headless=bool(args.headless and args.seconds is not None),
            storage_state=str(storage) if storage.exists() else None,
        )

        def on_response(response) -> None:
            request = response.request
            if request.resource_type not in ("xhr", "fetch"):
                return
            if NOISE.search(response.url):
                return
            content_type = (response.header_value("content-type") or "").lower()
            if "json" not in content_type:
                return

            try:
                body = response.json()
            except Exception:  # noqa: BLE001 - not parseable, not interesting
                return

            index = len(captures) + 1
            headers = request.all_headers()
            flagged = sorted(set(headers) & SENSITIVE_HEADERS)

            record = {
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "method": request.method,
                "url": response.url,
                "status": response.status,
                "request_headers": {
                    key: ("<redacted — see warning in README>" if key in SENSITIVE_HEADERS else value)
                    for key, value in headers.items()
                },
                "sensitive_headers_present": flagged,
                "request_body": _safe_post_data(request),
                "response_json": body,
            }

            name = _slug(response.url, index)
            (OUT_DIR / f"{name}.json").write_text(
                json.dumps(record, indent=2, ensure_ascii=False)[:2_000_000], "utf-8"
            )
            captures.append(
                {
                    "file": f"{name}.json",
                    "method": request.method,
                    "url": response.url,
                    "top_level_keys": list(body)[:12] if isinstance(body, dict) else f"<{type(body).__name__}>",
                    "sensitive_headers": flagged,
                }
            )
            marker = "  ⚠️ auth header" if flagged else ""
            print(f"[{index:03d}] {request.method} {response.url[:90]}{marker}")

        context.on("response", on_response)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(site["job_search_url"], timeout=browser_cfg["nav_timeout_ms"])

        if args.seconds is not None:
            # The results list fetches itself on load, so waiting is enough.
            deadline = time.monotonic() + args.seconds
            while time.monotonic() < deadline:
                page.wait_for_timeout(500)
        else:
            try:
                input("\nPress Enter when done capturing… ")
            except (EOFError, KeyboardInterrupt):
                pass

        browser_launch.close_context(browser, context)

    _write_index(captures)
    print(f"\n{len(captures)} JSON request(s) captured in {OUT_DIR}/")
    print(f"Start with {OUT_DIR}/index.md — find the one containing the job list.")
    return 0


def _safe_post_data(request) -> str | None:
    try:
        return (request.post_data or "")[:20000] or None
    except Exception:  # noqa: BLE001
        return None


def _write_index(captures: list[dict]) -> None:
    lines = [
        "# API captures",
        "",
        "Find the request whose response contains the job/shift list, then copy",
        "its URL, method, body, and headers into the `api:` block of config.yaml.",
        "",
    ]
    for entry in captures:
        lines.append(f"## {entry['file']}")
        lines.append(f"- `{entry['method']}` {entry['url']}")
        lines.append(f"- top-level keys: `{entry['top_level_keys']}`")
        if entry["sensitive_headers"]:
            lines.append(
                f"- ⚠️ needs {', '.join(entry['sensitive_headers'])} — these expire "
                "and rotate; a pasted value will stop working"
            )
        lines.append("")
    (OUT_DIR / "index.md").write_text("\n".join(lines), "utf-8")


if __name__ == "__main__":
    sys.exit(main())

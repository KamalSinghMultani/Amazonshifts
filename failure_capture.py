"""Best-effort local screenshots and safe metadata for browser failures.

Runtime artifacts go under screenshots/ (already gitignored).  The JSON sidecar
contains structural page evidence only; it deliberately never records cookie
values, localStorage values, credentials, OTPs, WAF parameters, or solver
responses.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import auth_evidence


SCREENSHOT_DIR = Path("screenshots")


def _slug(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(value or "failure")).strip("-.")
    return value[:60] or "failure"


def capture(page, context, base_url: str, category: str, *, extra: dict | None = None) -> dict:
    """Capture the exact page where a browser flow failed.

    Returns paths/evidence for logging.  Any capture error is represented in
    the returned dict instead of raising; diagnostics must never become the
    reason session recovery crashes.
    """
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
    stem = f"{_slug(category)}-{stamp}"
    png = SCREENSHOT_DIR / f"{stem}.png"
    sidecar = SCREENSHOT_DIR / f"{stem}.json"

    result: dict = {
        "screenshot": str(png),
        "sidecar": str(sidecar),
        "category": str(category or "failure"),
    }

    try:
        page.screenshot(path=str(png), full_page=True)
    except Exception as exc:  # noqa: BLE001
        result["screenshot_error"] = str(exc)[:240]
        result["screenshot"] = ""

    try:
        evidence = auth_evidence.collect(page, context, base_url)
    except Exception as exc:  # noqa: BLE001
        evidence = {"evidence_error": str(exc)[:240]}

    # URL values are useful for routing diagnosis but strip query parameters so
    # candidate/application identifiers are not copied into diagnostic JSON.
    try:
        parsed = urlparse(getattr(page, "url", "") or "")
        safe_location = f"{parsed.scheme}://{parsed.netloc}{parsed.path}#{parsed.fragment}"
    except Exception:
        safe_location = ""

    payload = {
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "category": str(category or "failure"),
        "safe_location": safe_location,
        "evidence": evidence,
        "extra": extra or {},
    }
    try:
        sidecar.write_text(json.dumps(payload, indent=2, default=str), "utf-8")
    except Exception as exc:  # noqa: BLE001
        result["sidecar_error"] = str(exc)[:240]
        result["sidecar"] = ""

    result["evidence"] = evidence
    return result

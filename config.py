"""Config loading, defaults, and validation.

Validation happens up front and loudly: a typo in config.yaml should fail at
startup, not silently at 3am when the shift you wanted goes by.
"""

from __future__ import annotations

import copy
import logging
import logging.handlers
import os
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

DEFAULTS: dict[str, Any] = {
    "dry_run": True,
    "site": {
        "base_url": "https://hiring.amazon.ca",
        "job_search_url": "https://hiring.amazon.ca/app#/jobSearch",
    },
    "polling": {
        "mode": "dom",
        # Raised from 20s after a live test: three page loads ~14s apart got a
        # CloudFront "Request blocked" 403. See config.yaml for the tradeoff.
        "interval_seconds": 45,
        "jitter_seconds": 20,
        # ── hot mode ──
        # Amazon posts shifts in batches: once one appears, the next is usually
        # seconds away. Poll faster during those windows only.
        "hot_interval_seconds": 20,
        "hot_duration_seconds": 120,
        "hot_windows": [],
        "max_consecutive_errors": 5,
        "cooldown_seconds": 300,
        # The site is a SPA; scraping right after domcontentloaded finds an
        # empty shell.
        "render_wait_ms": 5000,
    },
    "browser": {
        "headless": True,
        "storage_state": "auth_state.json",
        "nav_timeout_ms": 30000,
        "action_timeout_ms": 10000,
        "user_agent": None,
        "locale": "en-CA",
        "timezone": "America/Toronto",
        "executable_path": None,
        # See browser_launch.py — these exist to stop a legitimate manual
        # login being misread as a bot.
        "channel": "chrome",
        "user_data_dir": "browser_profile",
        "stealth": True,
    },
    "api": {
        "endpoint_url": None,
        "method": "POST",
        "payload": None,
        "shifts_path": "",
        "field_map": {},
        "url_template": None,
        "extra_headers": {},
        # The endpoint 401s without an authorization token that the page mints
        # and rotates. Harvest it live rather than pasting a copy that dies.
        "auth_from_page": True,
        "auth_header": "authorization",
        "auth_storage_key": "sessionToken",
    },
    "filters": {},
    # Which acceptable shift you want most. See ShiftRanker.
    "priority": {
        "order": ["location", "title", "pay"],
        "locations": [],
        "titles": [],
        "demote_titles": [],
    },
    "notifications": {
        "telegram": {"enabled": True, "screenshot": True},
        "notify_on_start": True,
        "notify_on_error": True,
        # A whole batch can land in one poll. Telegram rate-limits a single
        # chat, so past this many the rest become one summary line.
        "max_alerts_per_poll": 8,
    },
    # max_per_poll: you only need to win one shift; holding a whole batch
    # would race itself and multiply the clicks.
    "hold": {"enabled": True, "stop_before_submit": True, "max_per_poll": 1},
    "state": {
        "path": "state/seen_shifts.json",
        "ttl_hours": 72,
        # Append-only log of every detection, read back by --drop-report.
        "detections_path": "state/detections.jsonl",
    },
    "logging": {
        "level": "INFO",
        "path": "logs/watcher.log",
        "max_bytes": 2000000,
        "backup_count": 3,
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: str | os.PathLike = "config.yaml") -> dict:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"config file not found: {path}")
    raw = yaml.safe_load(path.read_text("utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain a YAML mapping at the top level")
    cfg = _deep_merge(DEFAULTS, raw)
    validate_config(cfg)
    return cfg


# ── hot windows ─────────────────────────────────────────────────────────────
# A window is "HH:MM-HH:MM" in your LOCAL time. It may wrap midnight
# ("22:00-02:00"), which is why they are stored as minute offsets and compared
# with a wrap-aware test rather than a simple start <= now <= end.
def _parse_clock(text: str, window: str) -> int:
    parts = str(text).strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"bad time {text!r} in hot window {window!r} — want HH:MM")
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        raise ValueError(f"bad time {text!r} in hot window {window!r} — want HH:MM") from None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"time out of range in hot window {window!r}: {text!r}")
    return hour * 60 + minute


def parse_hot_windows(windows: Any) -> list[tuple[int, int]]:
    """["06:00-09:00"] -> [(360, 540)]. Raises on anything malformed.

    Parsing happens at startup so a typo like "breakfast" fails immediately
    instead of quietly never opening a window — a silent no-op would look
    exactly like a day with no shifts.
    """
    if not windows:
        return []
    if isinstance(windows, str):
        windows = [windows]

    parsed: list[tuple[int, int]] = []
    for window in windows:
        text = str(window).strip()
        if text.count("-") != 1:
            raise ValueError(f"bad hot window {window!r} — want \"HH:MM-HH:MM\"")
        start_text, end_text = text.split("-")
        start = _parse_clock(start_text, text)
        end = _parse_clock(end_text, text)
        if start == end:
            raise ValueError(f"hot window {window!r} is zero-length")
        parsed.append((start, end))
    return parsed


def in_hot_window(now: Any, windows: list[tuple[int, int]]) -> bool:
    """Is `now` (a datetime) inside any window? Wrap-aware."""
    minutes = now.hour * 60 + now.minute
    for start, end in windows:
        if start < end:
            if start <= minutes < end:
                return True
        elif minutes >= start or minutes < end:  # wraps midnight
            return True
    return False


def validate_config(cfg: dict) -> None:
    mode = cfg["polling"]["mode"]
    if mode not in ("dom", "api"):
        raise ValueError(f"polling.mode must be 'dom' or 'api', got {mode!r}")

    polling = cfg["polling"]
    if polling["interval_seconds"] < 5:
        raise ValueError(
            "polling.interval_seconds below 5 is abusive to the site and will "
            "get you rate-limited or blocked"
        )

    if mode == "dom" and polling["interval_seconds"] < 30:
        # dom polls are full page loads. Measured: a 403 at ~14s apart.
        log.warning(
            "polling.interval_seconds=%s is risky in dom mode — a CloudFront "
            "403 was observed at ~14s between page loads. 45 is the tested "
            "value; the faster settings are for api mode.",
            polling["interval_seconds"],
        )

    hot = polling["hot_interval_seconds"]
    if hot < 3:
        raise ValueError(
            "polling.hot_interval_seconds below 3 will get you blocked long "
            "before it wins you a shift"
        )
    if hot > polling["interval_seconds"]:
        raise ValueError(
            "polling.hot_interval_seconds must be <= interval_seconds — "
            "hot mode is the fast cadence, not the slow one"
        )
    if mode == "dom" and hot < 20:
        # Measured, not guessed: three full page loads ~14s apart earned a
        # CloudFront 403. A blocked watcher finds nothing at all, which loses
        # more shifts than a slower poll ever will.
        log.warning(
            "polling.hot_interval_seconds=%s in dom mode risks a CloudFront "
            "block (a 403 was observed at ~14s between page loads). Configure "
            "api mode before polling this fast.",
            hot,
        )
    if hot * 1000 < polling.get("render_wait_ms", 0) and mode == "dom":
        log.warning(
            "hot_interval_seconds=%ss is shorter than render_wait_ms=%sms — "
            "each dom poll already takes longer than that, so the extra speed "
            "is imaginary",
            hot, polling.get("render_wait_ms"),
        )

    # Parse for the side effect: a malformed window must fail at startup.
    polling["hot_windows_parsed"] = parse_hot_windows(polling.get("hot_windows"))

    if mode == "api":
        api = cfg["api"]
        if not api.get("endpoint_url"):
            raise ValueError(
                "polling.mode is 'api' but api.endpoint_url is not set. "
                "Run `python api_sniffer.py` and fill in the api: block."
            )
        if not api.get("shifts_path"):
            raise ValueError("polling.mode is 'api' but api.shifts_path is not set")
        if not api.get("field_map"):
            raise ValueError("polling.mode is 'api' but api.field_map is empty")
        if str(api.get("method", "POST")).upper() not in ("GET", "POST"):
            raise ValueError("api.method must be GET or POST")

    if not cfg["dry_run"] and cfg["hold"]["enabled"] and not cfg["hold"]["stop_before_submit"]:
        log.warning(
            "hold.stop_before_submit is FALSE — this bot will submit "
            "applications with no human in the loop."
        )


def load_dotenv(path: str | os.PathLike = ".env") -> None:
    """Minimal .env loader so we do not need python-dotenv at runtime.

    Existing environment variables always win.
    """
    path = Path(path)
    if not path.exists():
        return
    for line in path.read_text("utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def setup_logging(cfg: dict) -> None:
    log_cfg = cfg.get("logging", {})
    level = getattr(logging, str(log_cfg.get("level", "INFO")).upper(), logging.INFO)
    handlers: list[logging.Handler] = [logging.StreamHandler()]

    log_path = log_cfg.get("path")
    if log_path:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                log_path,
                maxBytes=int(log_cfg.get("max_bytes", 2_000_000)),
                backupCount=int(log_cfg.get("backup_count", 3)),
                encoding="utf-8",
            )
        )

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
    # Playwright is chatty at DEBUG and drowns out our own lines.
    logging.getLogger("urllib3").setLevel(logging.WARNING)

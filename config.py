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
    },
    "filters": {},
    "notifications": {
        "telegram": {"enabled": True, "screenshot": True},
        "notify_on_start": True,
        "notify_on_error": True,
    },
    "hold": {"enabled": True, "stop_before_submit": True},
    "state": {"path": "state/seen_shifts.json", "ttl_hours": 72},
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


def validate_config(cfg: dict) -> None:
    mode = cfg["polling"]["mode"]
    if mode not in ("dom", "api"):
        raise ValueError(f"polling.mode must be 'dom' or 'api', got {mode!r}")

    if cfg["polling"]["interval_seconds"] < 5:
        raise ValueError(
            "polling.interval_seconds below 5 is abusive to the site and will "
            "get you rate-limited or blocked"
        )

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

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
    # warehouse_types / shift_types mirror Amazon's own onboarding choices.
    # Empty means every type, which is where its wizard starts too.
    "filters": {
        "warehouse_types": [],
        "shift_types": [],
        # An ISO timestamp. While it is in the future, every posting matches —
        # for proving the hold path when nothing commutable is up. It expires
        # on its own; see ShiftMatcher.
        "accept_everything_until": None,
    },
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
    # direct_apply: OFF, and the comment is the point. Deep-linking into
    # /application/?jobId=…&scheduleId=… looked like the big win — five page
    # loads collapsing into one — and it does not work. Tested 2026-08-18:
    # plain navigation, after warming /application/, with a Referer, via
    # window.open from the authenticated page, the post-redirect URL form, and
    # the consent stage directly. Every one bounces to the login page while
    # the same session loads /application/ fine. The app needs state that only
    # the click flow establishes. Left in place, and off, so nobody spends
    # another evening rediscovering this.
    "hold": {
        "enabled": True,
        "stop_before_submit": True,
        "max_per_poll": 1,
        "direct_apply": False,
        # Keep the application frontend loaded on a dedicated page so a match
        # does not pay the full cold-start cost of its React bundles.
        "prewarm_application": True,
        # Cold prewarm is outside the reservation race. Give Amazon's React
        # shell time to mount late cookie/banner overlays, then dismiss them
        # with ordinary browser actions so the first hold does not pay for it.
        "prewarm_overlay_settle_ms": 4500,
        # The original job-detail click path can add tens of seconds after a
        # direct attempt has already failed. Keep it available for diagnostics,
        # but never use it on the latency-first production path by default.
        "compatibility_fallback": False,
        # A slot is often gone between the flyout rendering and Apply landing.
        # Try the next schedule on the same job rather than abandoning the job.
        "schedule_attempts": 3,
        # ...and if every schedule on that job is gone, try the next-ranked
        # job. Losing Brampton to a faster service should cost that job, not the whole batch.
        "job_attempts": 3,
        # ...but not forever: a posting lasts about a minute, and time spent
        # retrying a dead job is time not spent on the next one.
        "attempt_budget_seconds": 45,
        # Explicit opt-in for the two identity-consent boxes and launcher on
        # already-verified accounts. Real remoteKYC controls stay manual.
        "auto_accept_identity_consent_and_start": False,
    },
    # Empty means "any schedule will do". Populate to constrain which one gets
    # taken: available_days, min_hours_per_week, avoid_overnight.
    "schedule_preferences": {},
    # The hiring portal session expires on its own — measured at roughly two
    # hours. Detection keeps working when it does, which is precisely the
    # danger: the watcher looks healthy and cannot hold a thing.
    #
    # The competing service's own FAQ says how they live with this: "To stay
    # active, the bot auto-logs in every 2 hours." They do not try to keep a
    # session alive; they replace it before it dies. So do we.
    "session": {
        "check_every_seconds": 600,
        "keepalive": True,
        "alert_on_expiry": True,
        # 100 minutes: inside the observed ~2h window with room for a slow
        # attempt. 0 disables the cycle and falls back to re-logging in only
        # once an expiry has been noticed — by which point a shift may already
        # have been missed.
        "relogin_every_seconds": 6000,
        # Repeated logins are the one way this can do harm. A 100-minute cycle
        # needs about 15 a day; 12 leaves the cycle intact while capping a
        # runaway loop, and Amazon challenging us repeatedly is exactly when
        # backing off matters most.
        "max_relogins_per_day": 12,
        # Opt-in. Needs AMAZON_LOGIN_EMAIL / AMAZON_LOGIN_PASSWORD in .env.
        # See relogin.py for why this is off by default.
        "auto_relogin": False,
    },
    "state": {
        "path": "state/seen_shifts.json",
        "ttl_hours": 72,
        # Append-only log of every detection, read back by --drop-report.
        "detections_path": "state/detections.jsonl",
    },
    "lifecycle_monitor": {
        "enabled": False,
        "interval_seconds": 2.0,
        "notify_unposted": True,
        "notify_posted_without_capacity": True,
        "state_path": "state/job_lifecycle.json",
        "events_path": "state/job_lifecycle_events.jsonl",
        "known_jobs": [],
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


def _load_raw(path: Path, seen: list[Path] | None = None) -> dict:
    """Read one config file, resolving `extends:` first.

    `extends` exists so a second environment — a US config for testing the
    workflow against a site that always has jobs, say — can override the five
    keys that differ instead of duplicating the whole file and quietly drifting
    out of sync with it.
    """
    seen = seen or []
    path = path.resolve()
    if path in seen:
        chain = " -> ".join(p.name for p in [*seen, path])
        raise ValueError(f"config extends itself in a loop: {chain}")
    if not path.exists():
        raise FileNotFoundError(f"config file not found: {path}")

    raw = yaml.safe_load(path.read_text("utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain a YAML mapping at the top level")

    parent = raw.pop("extends", None)
    if not parent:
        return raw

    # Relative to the child config, so configs can live in a subdirectory.
    base_path = Path(parent)
    if not base_path.is_absolute():
        base_path = path.parent / base_path
    base = _load_raw(base_path, [*seen, path])
    return _deep_merge(base, raw)


def load_config(path: str | os.PathLike = "config.yaml") -> dict:
    cfg = _deep_merge(DEFAULTS, _load_raw(Path(path)))
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
    interval = float(polling["interval_seconds"])
    hot = float(polling["hot_interval_seconds"])

    # A DOM poll is a full page load; keep the existing hard protection there.
    # API mode is a much smaller request. The shipped 1.25s/0.65s cadence is an
    # explicit experimental choice, so permit it but keep an absolute floor and
    # warn below the previously measured-clean 2s cadence. The normal circuit
    # breaker still handles 403/429/network failures by backing off.
    if mode == "dom":
        if interval < 20:
            raise ValueError(
                "polling.interval_seconds below 20 in dom mode risks a block, "
                "and a blocked watcher finds nothing at all"
            )
        if hot < 20:
            raise ValueError(
                "polling.hot_interval_seconds below 20 in dom mode risks repeated "
                "full-page loads and a block"
            )
    else:
        if interval < 0.5:
            raise ValueError(
                "polling.interval_seconds below 0.5 in api mode is too aggressive"
            )
        if hot < 0.5:
            raise ValueError(
                "polling.hot_interval_seconds below 0.5 in api mode is too aggressive"
            )
        if interval < 2:
            log.warning(
                "polling.interval_seconds=%s is below the previously measured-clean "
                "2s API cadence; this is experimental. Watch for 403/429/circuit-"
                "breaker events and back off if they appear.",
                interval,
            )
        if hot < 2:
            log.warning(
                "polling.hot_interval_seconds=%s is below the previously measured-clean "
                "2s API cadence; this is experimental. Watch for 403/429/circuit-"
                "breaker events and back off if they appear.",
                hot,
            )

    if mode == "dom" and interval < 30:
        # dom polls are full page loads. Measured: a 403 at ~14s apart.
        log.warning(
            "polling.interval_seconds=%s is risky in dom mode — a CloudFront "
            "403 was observed at ~14s between page loads. 45 is the tested "
            "value; the faster settings are for api mode.",
            interval,
        )

    if hot > interval:
        raise ValueError(
            "polling.hot_interval_seconds must be <= interval_seconds — "
            "hot mode is the fast cadence, not the slow one"
        )
    if hot * 1000 < polling.get("render_wait_ms", 0) and mode == "dom":
        log.warning(
            "hot_interval_seconds=%ss is shorter than render_wait_ms=%sms — "
            "each dom poll already takes longer than that, so the extra speed "
            "is imaginary",
            hot, polling.get("render_wait_ms"),
        )

    session = cfg.get("session") or {}
    if session.get("check_every_seconds") is not None:
        if int(session["check_every_seconds"]) < 60:
            raise ValueError(
                "session.check_every_seconds below 60 loads a page far more "
                "often than a session can plausibly expire"
            )

    relogin_every = int(session.get("relogin_every_seconds") or 0)
    if relogin_every and relogin_every < 600:
        # A login is not a health check. Signing in every few minutes would
        # earn a challenge, and a challenged account holds no shifts at all.
        raise ValueError(
            "session.relogin_every_seconds below 600 signs in far more often "
            "than any session expires, and invites a challenge"
        )
    if relogin_every and not session.get("auto_relogin"):
        log.warning(
            "session.relogin_every_seconds is set but auto_relogin is false — "
            "no scheduled re-login will happen"
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

    lifecycle = cfg.get("lifecycle_monitor") or {}
    if lifecycle.get("enabled"):
        if mode != "api":
            raise ValueError("lifecycle_monitor requires polling.mode: api")
        if float(lifecycle.get("interval_seconds", 2.0)) < 1.0:
            raise ValueError("lifecycle_monitor.interval_seconds below 1.0 is too aggressive")
        known = lifecycle.get("known_jobs") or []
        if not isinstance(known, list) or not known:
            raise ValueError("enabled lifecycle_monitor requires known_jobs")
        ids = []
        for item in known:
            if not isinstance(item, dict) or not str(item.get("job_id") or "").strip():
                raise ValueError("each lifecycle_monitor known job requires job_id")
            ids.append(str(item["job_id"]).strip())
        if len(ids) != len(set(ids)):
            raise ValueError("lifecycle_monitor known job ids must be unique")

    if not cfg["dry_run"] and cfg["hold"]["enabled"] and not cfg["hold"]["stop_before_submit"]:
        # Precise, because the difference matters: it creates an application
        # and reserves the shift, and stops. It does not fill in or submit the
        # 7-step form behind it.
        log.warning(
            "LIVE HOLD ENABLED — on a match this will press Create "
            "Application, accepting the 18+/drug-test declarations and "
            "reserving the shift for ~3 hours. It does not fill in or submit "
            "the application itself."
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

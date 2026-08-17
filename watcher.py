"""The watcher. Poll for shifts, alert on Telegram, optionally hold the slot.

    python watcher.py                    # uses config.yaml
    python watcher.py --check-selectors  # report unconfigured selectors and exit
    python watcher.py --once             # one poll, then exit (good for testing)
    python watcher.py --live             # override dry_run: false for this run

Safety model:
  * dry_run: true (the default) never clicks anything.
  * Even when live, it stops one step before the final submit — you finish the
    application yourself. See hold.stop_before_submit in config.yaml.
"""

from __future__ import annotations

import argparse
import logging
import random
import signal
import sys
import threading
from datetime import datetime
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

import browser_launch
import site_selectors
from api_client import ApiClient
from config import load_config, load_dotenv, setup_logging
from notifier import TelegramNotifier
from shift_matcher import ShiftMatcher
from state_store import StateStore

log = logging.getLogger("watcher")

SCREENSHOT_DIR = Path("screenshots")


class Watcher:
    def __init__(self, cfg: dict, live_override: bool = False) -> None:
        self.cfg = cfg
        self.dry_run = False if live_override else bool(cfg["dry_run"])
        self.mode = cfg["polling"]["mode"]

        self.matcher = ShiftMatcher(cfg.get("filters"))
        self.state = StateStore(cfg["state"]["path"], cfg["state"]["ttl_hours"])

        telegram_cfg = cfg["notifications"]["telegram"]
        self.notifier = TelegramNotifier(
            enabled=telegram_cfg.get("enabled", True),
            send_screenshots=telegram_cfg.get("screenshot", True),
        )

        self.stop_event = threading.Event()
        self.consecutive_errors = 0
        self.polls = 0
        self.alerts = 0

        self.context = None
        self.page = None
        self.api_client: ApiClient | None = None

    # ── lifecycle ───────────────────────────────────────────────────────────
    def request_stop(self, signum, _frame) -> None:
        log.info("received signal %s — finishing current poll and shutting down", signum)
        self.stop_event.set()

    def run(self, once: bool = False) -> int:
        browser_cfg = self.cfg["browser"]
        storage = Path(browser_cfg["storage_state"])
        profile = browser_cfg.get("user_data_dir")
        # Either source of session is enough: a persistent profile carries the
        # login on its own, and does not need auth_state.json.
        has_profile = bool(profile) and Path(profile).exists()
        if not storage.exists() and not has_profile:
            log.error(
                "no saved session (looked for %s and profile %s) — "
                "run `python save_session.py` first",
                storage, profile or "<disabled>",
            )
            return 2

        signal.signal(signal.SIGINT, self.request_stop)
        signal.signal(signal.SIGTERM, self.request_stop)

        with sync_playwright() as playwright:
            log.info("browser: %s", browser_launch.describe(browser_cfg))
            browser, self.context = browser_launch.launch_context(
                playwright,
                browser_cfg,
                storage_state=str(storage) if storage.exists() else None,
            )
            self.context.set_default_timeout(browser_cfg["action_timeout_ms"])
            self.context.set_default_navigation_timeout(browser_cfg["nav_timeout_ms"])

            if self.mode == "api":
                self.api_client = ApiClient(self.context.request, self.cfg["api"])
            else:
                # dom mode keeps one page open and reloads it each poll.
                self.page = (
                    self.context.pages[0] if self.context.pages else self.context.new_page()
                )
                self.page.goto(self.cfg["site"]["job_search_url"])

            self._announce_start()
            try:
                self._loop(once=once)
            finally:
                self.state.save()
                browser_launch.close_context(browser, self.context)

        log.info(
            "stopped after %d poll(s), %d alert(s), %d shift(s) remembered",
            self.polls, self.alerts, len(self.state),
        )
        return 0

    def _announce_start(self) -> None:
        mode_note = "DRY RUN — detect and alert only" if self.dry_run else (
            "LIVE — will hold slots, stopping before submit"
            if self.cfg["hold"]["stop_before_submit"]
            else "LIVE — FULLY AUTOMATED, will submit applications"
        )
        log.info("watcher started | mode=%s | %s", self.mode, mode_note)
        if self.cfg["notifications"].get("notify_on_start"):
            self.notifier.send_text(
                f"👀 <b>Shift watcher started</b>\n"
                f"detection: <code>{self.mode}</code>\n{mode_note}"
            )

    # ── main loop ───────────────────────────────────────────────────────────
    def _loop(self, once: bool = False) -> None:
        polling = self.cfg["polling"]
        while not self.stop_event.is_set():
            try:
                self.poll_once()
                self.consecutive_errors = 0
            except Exception as exc:  # noqa: BLE001 - the loop must survive anything
                self.consecutive_errors += 1
                log.exception("poll failed (%d in a row)", self.consecutive_errors)
                if self.consecutive_errors >= polling["max_consecutive_errors"]:
                    self._trip_circuit_breaker(exc)

            if once or self.stop_event.is_set():
                break

            delay = polling["interval_seconds"] + random.uniform(0, polling["jitter_seconds"])
            log.debug("sleeping %.1fs", delay)
            self.stop_event.wait(delay)

    def _trip_circuit_breaker(self, exc: Exception) -> None:
        cooldown = self.cfg["polling"]["cooldown_seconds"]
        log.error("circuit breaker tripped — cooling down for %ds", cooldown)
        if self.cfg["notifications"].get("notify_on_error"):
            self.notifier.notify_error(
                f"{self.consecutive_errors} consecutive failures, "
                f"pausing {cooldown}s.\nLast error: {exc}"
            )
        self.stop_event.wait(cooldown)
        self.consecutive_errors = 0

    def poll_once(self) -> None:
        self.polls += 1
        shifts = self._fetch_shifts()
        log.info("poll %d: %d shift(s) visible", self.polls, len(shifts))

        for shift in shifts:
            matched, reason = self.matcher.matches(shift)
            if not matched:
                log.debug("skip %s (%s)", shift.summary(), reason)
                continue
            if self.state.has_seen(shift.stable_id):
                log.debug("already alerted: %s", shift.summary())
                continue

            # Mark + persist BEFORE acting. If the hold crashes we would rather
            # miss a retry than spam the same alert on every poll.
            self.state.mark_seen(shift.stable_id, shift.summary())
            self.state.save()
            self.alerts += 1

            log.info("MATCH: %s", shift.summary())
            # Alert first, always. In api mode nothing has touched the browser
            # yet, so this fires within milliseconds of the shift appearing.
            self.notifier.notify_shift(shift, dry_run=self.dry_run)

            if self.dry_run:
                log.info("dry run — not clicking")
                continue
            if not self.cfg["hold"]["enabled"]:
                log.info("hold disabled — alert only")
                continue

            self._hold(shift)

    def _fetch_shifts(self):
        if self.mode == "api":
            assert self.api_client is not None
            return self.api_client.fetch_shifts()

        assert self.page is not None
        try:
            self.page.reload(wait_until="domcontentloaded")
        except PlaywrightError:
            # A reload can fail on a hash-routed SPA; a fresh goto recovers it.
            self.page.goto(self.cfg["site"]["job_search_url"], wait_until="domcontentloaded")
        self._warn_if_logged_out(self.page)
        return site_selectors.extract_shifts(self.page)

    def _warn_if_logged_out(self, page) -> None:
        url = (page.url or "").lower()
        if any(marker in url for marker in ("login", "signin", "sign-in")):
            log.error("looks like the session expired — re-run save_session.py")
            if self.cfg["notifications"].get("notify_on_error"):
                self.notifier.notify_error(
                    "Session appears to have expired — re-run save_session.py"
                )

    # ── holding ─────────────────────────────────────────────────────────────
    def _hold(self, shift) -> None:
        page = self.page
        if page is None:
            # api mode: this is the first time we need a browser page at all.
            page = self.context.new_page()
            self.page = page

        target = shift.url or self.cfg["site"]["job_search_url"]
        try:
            page.goto(target, wait_until="domcontentloaded")
        except PlaywrightError as exc:
            log.error("could not open %s: %s", target, exc)
            self.notifier.notify_error(f"Could not open the listing: {exc}")
            return

        SCREENSHOT_DIR.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        shot = SCREENSHOT_DIR / f"hold-{stamp}.png"

        ok, message = site_selectors.hold_shift(
            page,
            shift,
            stop_before_submit=self.cfg["hold"]["stop_before_submit"],
            timeout_ms=self.cfg["browser"]["action_timeout_ms"],
            screenshot_path=str(shot),
        )

        if ok:
            log.info("hold succeeded: %s", message)
            self.notifier.notify_held(shift, self.cfg["hold"]["stop_before_submit"])
            self.notifier.send_photo(shot, caption=message)
        else:
            log.error("hold failed: %s", message)
            self.notifier.notify_error(f"Hold failed for {shift.summary()}\n{message}")


def check_selectors() -> int:
    missing = site_selectors.unconfigured()
    if not missing:
        print("✅ all selectors are configured")
        return 0
    print("❌ these selectors are still placeholders in site_selectors.py:\n")
    for name in missing:
        print(f"   - {name}")
    print(
        "\nFill them in with:\n"
        "   python -m playwright codegen --load-storage=auth_state.json \\\n"
        "          https://hiring.amazon.ca/app#/jobSearch\n"
        "\ndom mode and holding will not work until these are set. "
        "api mode can detect shifts without them, but cannot hold."
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Amazon shift watcher")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--once", action="store_true", help="one poll, then exit")
    parser.add_argument("--live", action="store_true", help="override dry_run for this run")
    parser.add_argument("--check-selectors", action="store_true")
    args = parser.parse_args(argv)

    if args.check_selectors:
        return check_selectors()

    load_dotenv()
    cfg = load_config(args.config)
    setup_logging(cfg)

    if cfg["polling"]["mode"] == "dom" and not site_selectors.selectors_ready():
        log.error("dom mode needs real selectors — run `python watcher.py --check-selectors`")
        return 2

    return Watcher(cfg, live_override=args.live).run(once=args.once)


if __name__ == "__main__":
    sys.exit(main())

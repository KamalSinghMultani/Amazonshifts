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
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

import browser_launch
import site_selectors
from api_client import ApiClient
from auth_token import TokenSource
import doctor
import drop_report
from config import (
    in_hot_window,
    load_config,
    load_dotenv,
    parse_hot_windows,
    setup_logging,
)
from notifier import TelegramNotifier
from shift_matcher import ShiftMatcher, ShiftRanker
from state_store import StateStore

log = logging.getLogger("watcher")

SCREENSHOT_DIR = Path("screenshots")


class Watcher:
    def __init__(self, cfg: dict, live_override: bool = False) -> None:
        self.cfg = cfg
        self.dry_run = False if live_override else bool(cfg["dry_run"])
        self.mode = cfg["polling"]["mode"]

        self.matcher = ShiftMatcher(cfg.get("filters"))
        self.ranker = ShiftRanker(cfg.get("priority"))
        self.state = StateStore(
            cfg["state"]["path"],
            cfg["state"]["ttl_hours"],
            detections_path=cfg["state"].get("detections_path"),
        )

        # Hot mode: poll fast inside a configured window, and for a while after
        # any match, because Amazon posts in batches.
        polling = cfg["polling"]
        self.hot_windows = polling.get("hot_windows_parsed") or parse_hot_windows(
            polling.get("hot_windows")
        )
        self.hot_until = 0.0

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
        self.token_source = None

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
                self._start_api_mode(browser_cfg)
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

    def _start_api_mode(self, browser_cfg: dict) -> None:
        """Set up JSON polling, and the live token it needs.

        A page is opened even though api mode does not scrape one: the endpoint
        401s without an `authorization` token, and that token is minted by the
        page's own JavaScript and rotates. Keeping a page open is what makes a
        *fresh* token available on every poll instead of a pasted one that dies
        silently. It is also the page the hold flow will use later, so it is not
        wasted either way.
        """
        api_cfg = self.cfg["api"]
        token_source = None

        if api_cfg.get("auth_from_page", True):
            page = self.context.pages[0] if self.context.pages else self.context.new_page()
            self.page = page
            token_source = TokenSource(
                page,
                endpoint_url=api_cfg["endpoint_url"],
                header=api_cfg.get("auth_header", "authorization"),
                storage_key=api_cfg.get("auth_storage_key"),
                reload_url=self.cfg["site"]["job_search_url"],
                settle_ms=self.cfg["polling"].get("render_wait_ms", 5000),
            )
            page.goto(self.cfg["site"]["job_search_url"], wait_until="domcontentloaded")
            page.wait_for_timeout(self.cfg["polling"].get("render_wait_ms", 5000))
            if token_source.current():
                log.info("captured a live auth token from the page")
            else:
                log.warning(
                    "no auth token seen yet — if the endpoint needs one, the "
                    "first poll will 401 and refresh it"
                )

        self.token_source = token_source
        self.api_client = ApiClient(
            self.context.request,
            api_cfg,
            timeout_ms=self.cfg["browser"]["action_timeout_ms"],
            token_provider=token_source.current if token_source else None,
            on_unauthorized=token_source.refresh if token_source else None,
        )

    def _announce_start(self) -> None:
        mode_note = "DRY RUN — detect and alert only" if self.dry_run else (
            "LIVE — will hold slots, stopping before submit"
            if self.cfg["hold"]["stop_before_submit"]
            else "LIVE — FULLY AUTOMATED, will submit applications"
        )
        polling = self.cfg["polling"]
        windows = polling.get("hot_windows") or []
        log.info(
            "watcher started | mode=%s | %s | poll %ss (hot %ss%s)",
            self.mode, mode_note,
            polling["interval_seconds"], polling["hot_interval_seconds"],
            f", windows {', '.join(str(w) for w in windows)}" if windows else "",
        )
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

            delay, hot = self._next_delay()
            log.debug("sleeping %.1fs%s", delay, " [hot]" if hot else "")
            self.stop_event.wait(delay)

    # ── hot mode ────────────────────────────────────────────────────────────
    def is_hot(self, now: datetime | None = None) -> bool:
        """Should we be on the fast cadence right now?

        Two independent triggers: a configured clock window, or the tail of a
        recent match. The second is the one that exploits batching — the moment
        one shift appears, the next is usually seconds away, not minutes.
        """
        now = now or datetime.now()
        if in_hot_window(now, self.hot_windows):
            return True
        return now.timestamp() < self.hot_until

    def go_hot(self) -> None:
        duration = self.cfg["polling"]["hot_duration_seconds"]
        self.hot_until = max(self.hot_until, datetime.now().timestamp() + duration)

    def _next_delay(self) -> tuple[float, bool]:
        polling = self.cfg["polling"]
        hot = self.is_hot()
        if not hot:
            return (
                polling["interval_seconds"] + random.uniform(0, polling["jitter_seconds"]),
                False,
            )
        # Keep some jitter even when hot — a metronome is the easiest possible
        # traffic pattern to spot — but never enough to undo the speedup.
        base = polling["hot_interval_seconds"]
        jitter = min(polling["jitter_seconds"], base * 0.3)
        return base + random.uniform(0, jitter), True

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
        # You cannot tune what you do not measure — and the whole point of this
        # tool is latency, so every poll reports its own.
        started = time.perf_counter()
        hot = self.is_hot()
        shifts = self._fetch_shifts()
        fetch_ms = (time.perf_counter() - started) * 1000
        log.info(
            "poll %d: %d shift(s) in %.0fms%s",
            self.polls, len(shifts), fetch_ms, " [hot]" if hot else "",
        )

        # Canadian postings are rare and short-lived, so every one that shows
        # up is worth a line in the log even when it is filtered out. Without
        # this, a sighting leaves no trace and you cannot answer the obvious
        # question afterwards: was that one of mine?
        rejected: list[str] = []

        new_matches = []
        for shift in shifts:
            matched, reason = self.matcher.matches(shift)
            if not matched:
                log.debug("skip %s (%s)", shift.summary(), reason)
                rejected.append(f"{shift.summary()} [{reason}]")
                continue
            if self.state.has_seen(shift.stable_id):
                log.debug("already alerted: %s", shift.summary())
                continue

            # Mark BEFORE acting. If the hold crashes we would rather miss a
            # retry than spam the same alert on every poll.
            self.state.mark_seen(shift.stable_id, shift.summary())
            new_matches.append(shift)

        if rejected:
            log.info(
                "%d posting(s) seen but filtered out: %s",
                len(rejected), " | ".join(rejected[:5]),
            )

        if not new_matches:
            return

        self.state.save()
        # A match means a batch is probably landing — speed up regardless of
        # what happens with the alerts or the hold below.
        self.go_hot()

        # Best first. A whole batch can land in one poll, and both caps below
        # keep only the front of this list, so this ordering decides which
        # shift you hear about and which one actually gets held.
        new_matches = self.ranker.sort(new_matches)

        alert_cap = self.cfg["notifications"].get("max_alerts_per_poll") or len(new_matches)
        for index, shift in enumerate(new_matches):
            self.state.log_detection(shift.stable_id, shift.summary())
            self.alerts += 1
            log.info("MATCH: %s [%s]", shift.summary(), self.ranker.explain(shift))

            if index >= alert_cap:
                continue
            # Alert first, always. In api mode nothing has touched the browser
            # yet, so this fires within milliseconds of the shift appearing.
            self.notifier.notify_shift(shift, dry_run=self.dry_run)
            log.info(
                "alert sent %.0fms after poll start",
                (time.perf_counter() - started) * 1000,
            )

        # One digest instead of a hundred pings. Telegram rate-limits a single
        # chat, so an unfiltered batch would arrive slowly, out of order, and
        # bury the one shift that mattered.
        held_back = len(new_matches) - alert_cap
        if held_back > 0:
            log.info("%d further match(es) summarised rather than sent individually", held_back)
            self.notifier.send_text(
                f"➕ <b>{held_back} more match(es)</b> this poll — "
                f"see the log, or tighten <code>filters</code> in config.yaml."
            )

        if self.dry_run:
            log.info("dry run — not clicking")
            return
        if not self.cfg["hold"]["enabled"]:
            log.info("hold disabled — alert only")
            return

        # You need to win ONE shift. Trying to hold a whole batch would race
        # itself, and every extra click is another chance to be flagged.
        hold_cap = max(1, int(self.cfg["hold"].get("max_per_poll", 1)))
        for shift in new_matches[:hold_cap]:
            self._hold(shift)
        if len(new_matches) > hold_cap:
            log.info(
                "%d other match(es) alerted but not held (hold.max_per_poll=%d)",
                len(new_matches) - hold_cap, hold_cap,
            )

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

        # The SPA renders after domcontentloaded; scraping immediately finds
        # an empty shell.
        self.page.wait_for_timeout(self.cfg["polling"].get("render_wait_ms", 5000))

        state, detail = site_selectors.page_state(self.page)

        if state == "stale":
            # The access token expires quickly, but a reload refreshes it.
            # Verified: a second load comes back clean.
            log.info("token expired (%s) — reloading once to refresh it", detail)
            self.page.reload(wait_until="domcontentloaded")
            self.page.wait_for_timeout(self.cfg["polling"].get("render_wait_ms", 5000))
            state, detail = site_selectors.page_state(self.page)

        if state == "captcha":
            # Only a human can clear this. Alert loudly rather than sitting
            # there reporting zero shifts.
            log.error("a CAPTCHA is on screen — this needs you (%s)", detail)
            if self.cfg["notifications"].get("notify_on_error"):
                self.notifier.notify_error(
                    "A CAPTCHA is blocking the watcher. Run save_session.py and "
                    "clear it by hand, or set browser.headless: false to solve it live."
                )
            raise RuntimeError(f"captcha challenge ({detail})")

        if state == "blocked":
            # Raise so the circuit breaker counts it and backs off. Never treat
            # a WAF block as "no shifts today".
            raise RuntimeError(
                f"blocked by Amazon's WAF/CloudFront ({detail}). "
                "Polling too fast, or the browser looks automated."
            )

        if state == "login":
            self._report_logged_out(detail)
            raise RuntimeError(f"session expired ({detail})")

        return site_selectors.extract_shifts(self.page)

    def _report_logged_out(self, detail: str) -> None:
        log.error("looks like the session expired — re-run save_session.py (%s)", detail)
        if self.cfg["notifications"].get("notify_on_error"):
            self.notifier.notify_error(
                "Session appears to have expired — re-run save_session.py"
            )

    # ── holding ─────────────────────────────────────────────────────────────
    def _hold(self, shift) -> None:
        missing = site_selectors.unconfigured_hold()
        if missing:
            # Bail before navigating: opening the listing and then failing on
            # the first click wastes the seconds that matter most, and lands
            # the browser on a job page the next poll has to navigate back off.
            log.error("cannot hold — unconfigured: %s", ", ".join(missing))
            self.notifier.notify_error(
                "Matched a shift but cannot hold it — the apply-flow selectors "
                f"are still placeholders ({', '.join(missing)}). Open the "
                "listing yourself."
            )
            return

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
            self.notifier.notify_held(
                shift, self.cfg["hold"]["stop_before_submit"], detail=message
            )
            self.notifier.send_photo(shot, caption=message[:1000])
        else:
            log.error("hold failed: %s", message)
            self.notifier.notify_error(f"Hold failed for {shift.summary()}\n{message}")


def _use_utf8_console() -> None:
    """Windows terminals default to cp1252, which cannot encode the emoji this
    program prints and logs. Without this, `--check-selectors` dies with a
    UnicodeEncodeError and every emoji log line raises inside logging."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):  # not a real console; nothing to do
            pass


def check_selectors() -> int:
    detection = site_selectors.unconfigured_detection()
    hold = site_selectors.unconfigured_hold()

    if not detection and not hold:
        print("✅ all selectors are configured")
        return 0

    if detection:
        print("❌ detection is not configured — the watcher cannot see shifts:\n")
        for name in detection:
            print(f"   - {name}")
    else:
        print("✅ detection selectors are configured — dry-run watching works\n")

    if hold:
        print("\n⚠️  holding is not configured — detection and alerts still work,")
        print("   but the click-through will refuse to run:\n")
        for name in hold:
            print(f"   - {name}")

    print(
        "\nFill them in with:\n"
        "   python -m playwright codegen --load-storage=auth_state.json \\\n"
        "          https://hiring.amazon.ca/app#/jobSearch"
    )
    return 1 if detection else 0


def run_doctor(cfg: dict) -> int:
    """Check an environment end to end without needing a job to be posted."""
    checks = list(doctor.check_selectors())
    browser_cfg = cfg["browser"]
    storage = Path(browser_cfg["storage_state"])
    profile = browser_cfg.get("user_data_dir")
    has_profile = bool(profile) and Path(profile).exists()
    checks.append(doctor.Check(
        "saved session",
        doctor.OK if (storage.exists() or has_profile) else doctor.FAIL,
        f"{storage if storage.exists() else ''} "
        f"{profile if has_profile else ''}".strip() or "none found",
        fix="python save_session.py",
    ))

    with sync_playwright() as playwright:
        browser, context = browser_launch.launch_context(
            playwright, browser_cfg,
            storage_state=str(storage) if storage.exists() else None,
        )
        context.set_default_timeout(browser_cfg["action_timeout_ms"])
        context.set_default_navigation_timeout(browser_cfg["nav_timeout_ms"])
        page = context.pages[0] if context.pages else context.new_page()

        try:
            checks.append(doctor.check_job_search(page, cfg["site"]["job_search_url"]))

            if cfg["polling"]["mode"] == "api":
                token_source = TokenSource(
                    page,
                    endpoint_url=cfg["api"]["endpoint_url"],
                    header=cfg["api"].get("auth_header", "authorization"),
                    storage_key=cfg["api"].get("auth_storage_key"),
                    reload_url=cfg["site"]["job_search_url"],
                )
                page.reload(wait_until="domcontentloaded")
                page.wait_for_timeout(cfg["polling"].get("render_wait_ms", 5000))
                client = ApiClient(
                    context.request, cfg["api"],
                    token_provider=token_source.current,
                    on_unauthorized=token_source.refresh,
                )
                checks.extend(doctor.check_api(client, token_source))

            # Last, because it navigates away from the job search page.
            checks.append(doctor.check_portal_login(page, cfg["site"]["base_url"]))
        finally:
            browser_launch.close_context(browser, context)

    print(doctor.render(checks, f"Environment check — {cfg['site']['base_url']}"))
    return doctor.verdict(checks)[0]


def print_drop_report(cfg: dict) -> int:
    state = StateStore(
        cfg["state"]["path"],
        cfg["state"]["ttl_hours"],
        detections_path=cfg["state"].get("detections_path"),
    )
    entries = state.read_detections()
    print(drop_report.render(entries))

    # Anything this report prints must actually load. Validating the suggestion
    # with the same parser config.yaml uses means a paste can never produce a
    # window that silently never opens.
    suggested = drop_report.suggest_windows(drop_report.hourly_counts(entries))
    if suggested:
        parse_hot_windows(suggested)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Amazon shift watcher")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--once", action="store_true", help="one poll, then exit")
    parser.add_argument("--live", action="store_true", help="override dry_run for this run")
    parser.add_argument("--check-selectors", action="store_true")
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="check this environment end to end — session, login, API, "
             "selectors — without needing a job to be posted",
    )
    parser.add_argument(
        "--drop-report",
        action="store_true",
        help="when do shifts actually appear? reads your own detection log",
    )
    args = parser.parse_args(argv)

    _use_utf8_console()

    if args.check_selectors:
        return check_selectors()

    load_dotenv()
    cfg = load_config(args.config)

    if args.drop_report:
        return print_drop_report(cfg)

    setup_logging(cfg)

    if args.doctor:
        return run_doctor(cfg)

    if cfg["polling"]["mode"] == "dom" and not site_selectors.detection_ready():
        log.error(
            "dom mode needs the detection selectors — run "
            "`python watcher.py --check-selectors`"
        )
        return 2

    missing_hold = site_selectors.unconfigured_hold()
    if missing_hold:
        # Not fatal: detection and alerting are the point, and the dry-run
        # period is what you do *before* capturing the apply flow.
        log.warning(
            "holding is disabled — these are still placeholders: %s",
            ", ".join(missing_hold),
        )

    return Watcher(cfg, live_override=args.live).run(once=args.once)


if __name__ == "__main__":
    sys.exit(main())

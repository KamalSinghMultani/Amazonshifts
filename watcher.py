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
import relogin
import schedules as schedules_mod
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
        # Telegram sends happen off the critical path. A send has been seen to
        # take 10s on a cold connection, and every one of those seconds is a
        # second the shift is not reserved. Deferring them until after the hold
        # would be worse than either: if the hold hangs or the process dies,
        # you would hear nothing at all. So they go out in parallel.
        self._senders: list[threading.Thread] = []

        # Session watch. The portal signs itself out after a couple of hours
        # while job search carries on working, so nothing about a normal poll
        # reveals that holding has become impossible.
        session_cfg = cfg.get("session") or {}
        self.session_check_every = float(session_cfg.get("check_every_seconds") or 0)
        self.session_keepalive = bool(session_cfg.get("keepalive", True))
        self.alert_on_expiry = bool(session_cfg.get("alert_on_expiry", True))
        self.next_session_check = 0.0
        self.session_ok: bool | None = None
        self.auto_relogin = bool(session_cfg.get("auto_relogin", False))
        # One attempt per expiry. Repeated failed logins are how accounts get
        # locked, and a locked account costs every shift, not one.
        self.relogin_tried = False

        self.context = None
        self.page = None
        self.api_client: ApiClient | None = None
        self.token_source = None

    # ── notifications, off the critical path ────────────────────────────────
    def notify_async(self, method, *args, **kwargs) -> None:
        """Fire a Telegram send on its own thread.

        The notifier already swallows its own exceptions, so nothing here can
        take the watcher down. Threads are tracked so a shutdown can give them
        a moment to finish rather than cutting an alert off mid-flight.
        """
        thread = threading.Thread(
            target=method, args=args, kwargs=kwargs, daemon=True,
            name=f"notify-{getattr(method, '__name__', 'send')}",
        )
        thread.start()
        self._senders = [t for t in self._senders if t.is_alive()]
        self._senders.append(thread)

    def drain_notifications(self, timeout: float = 10.0) -> None:
        for thread in list(self._senders):
            thread.join(timeout=timeout)

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
                # Let any in-flight Telegram send finish. Without this a
                # --once run can exit before the alert leaves the machine.
                self.drain_notifications()
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
            # Accuracy matters here: neither mode submits an application. One
            # stops short of holding anything, the other reserves the slot and
            # leaves the 7-step form to you.
            "LIVE — walks to the consent screen but does NOT hold the slot"
            if self.cfg["hold"]["stop_before_submit"]
            else "LIVE — will HOLD slots (presses Create Application; you finish the steps)"
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

            self.check_session_if_due()

            if once or self.stop_event.is_set():
                break

            delay, hot = self._next_delay()
            log.debug("sleeping %.1fs%s", delay, " [hot]" if hot else "")
            self.stop_event.wait(delay)

    # ── session watch ───────────────────────────────────────────────────────
    def session_age(self) -> float | None:
        """Seconds since the last login, or None if it cannot be told.

        Taken from the mtime of the saved session, which save_session.py writes
        when you finish logging in. Approximate by nature, and still the only
        way to answer the question that decides whether this thing can run
        overnight: how long does a session actually last?
        """
        try:
            path = Path(self.cfg["browser"]["storage_state"])
            if path.exists():
                return time.time() - path.stat().st_mtime
        except OSError as exc:  # noqa: BLE001 - diagnostics only
            log.debug("could not read session age: %s", exc)
        return None

    @staticmethod
    def format_age(seconds: float | None) -> str:
        if seconds is None:
            return "unknown age"
        hours, remainder = divmod(int(seconds), 3600)
        return f"{hours}h{remainder // 60:02d}m old"

    def session_check_due(self, now: float | None = None) -> bool:
        if self.session_check_every <= 0 or self.dry_run:
            # In dry run nothing will be held anyway, so an expired session
            # costs nothing and does not deserve an alert.
            return False
        return (now if now is not None else time.monotonic()) >= self.next_session_check

    def check_session_if_due(self) -> bool | None:
        """Confirm the hiring portal is still signed in, and say so if not.

        Loading /application/ doubles as the keepalive: an idle session is what
        expires, and this is the same page the hold flow needs anyway.
        """
        if not self.session_check_due():
            return None
        self.next_session_check = time.monotonic() + self.session_check_every

        page = None
        try:
            page = self.context.new_page()
            check = doctor.check_portal_login(page, self.cfg["site"]["base_url"])
            signed_in = check.state == doctor.OK
        except Exception as exc:  # noqa: BLE001 - a check must never kill the loop
            log.warning("session check failed: %s", exc)
            return None
        finally:
            if page is not None:
                try:
                    page.close()
                except Exception:  # noqa: BLE001
                    pass

        was = self.session_ok
        self.session_ok = signed_in

        if signed_in:
            self.relogin_tried = False  # armed again for the next expiry
            if was is False:
                log.info("hiring portal session is back")
                if self.alert_on_expiry:
                    self.notify_async(
                        self.notifier.send_text,
                        "\u2705 <b>Session restored</b> — holding works again.",
                    )
            else:
                # At INFO, not DEBUG: this line accumulating in the log is the
                # measurement of how long a session survives.
                log.info("session check: signed in (%s)", self.format_age(self.session_age()))
            return True

        age = self.format_age(self.session_age())
        log.error(
            "hiring portal session has EXPIRED after %s — detection works, "
            "holding will not", age,
        )
        if self.auto_relogin and not self.relogin_tried:
            self.relogin_tried = True
            if self.try_relogin():
                return True

        # Once per expiry, not once per check: an hourly nag you learn to
        # ignore is worse than no alert at all.
        if was is not False and self.alert_on_expiry:
            self.notify_async(
                self.notifier.send_text,
                "\u26a0\ufe0f <b>Amazon session expired</b>\n"
                f"It lasted <b>{age}</b>.\n"
                "Shifts will still be detected and alerted, but the watcher "
                "<b>cannot hold one</b> until you log in again:\n"
                "<code>python save_session.py</code>",
            )
        return False

    def try_relogin(self) -> bool:
        """One automated sign-in attempt. Returns True only if verified.

        Verified means checked afterwards, not inferred from the attempt
        returning without error — the whole point of this project is not
        believing a click worked because it did not throw.
        """
        if relogin.credentials() is None:
            log.warning(
                "session.auto_relogin is on but AMAZON_LOGIN_EMAIL / "
                "AMAZON_LOGIN_PASSWORD are not in .env"
            )
            return False

        log.info("attempting an automated re-login")
        page = None
        try:
            page = self.context.new_page()
            status, detail = relogin.attempt(page, self.cfg["site"]["base_url"])
            log.info("re-login attempt: %s (%s)", status, detail)

            if status != relogin.OK:
                self.notify_async(
                    self.notifier.notify_error,
                    f"Automated re-login could not finish: {detail}\n"
                    "Run <code>python save_session.py</code> when you can.",
                )
                return False

            check = doctor.check_portal_login(page, self.cfg["site"]["base_url"])
            if check.state != doctor.OK:
                log.warning("re-login claimed success but the portal disagrees")
                return False
        except Exception as exc:  # noqa: BLE001 - never take the loop down
            log.warning("re-login attempt raised: %s", exc)
            return False
        finally:
            if page is not None:
                try:
                    page.close()
                except Exception:  # noqa: BLE001
                    pass

        # Persist it, so a restart does not have to do this all over again.
        try:
            self.context.storage_state(path=self.cfg["browser"]["storage_state"])
        except Exception as exc:  # noqa: BLE001
            log.debug("could not save the refreshed session: %s", exc)

        self.session_ok = True
        log.info("automated re-login succeeded — holding works again")
        self.notify_async(
            self.notifier.send_text,
            "\U0001f501 <b>Session refreshed automatically</b> — holding works again.",
        )
        return True

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

        for shift in new_matches:
            self.state.log_detection(shift.stable_id, shift.summary())
            self.alerts += 1
            log.info("MATCH: %s [%s]", shift.summary(), self.ranker.explain(shift))

        holding = (
            not self.dry_run
            and self.cfg["hold"]["enabled"]
            and site_selectors.detection_ready()
        )
        alert_cap = self.cfg["notifications"].get("max_alerts_per_poll") or len(new_matches)

        if not holding:
            # Nothing is going to be clicked, so there is no critical path to
            # protect: alert normally, best first.
            for shift in new_matches[:alert_cap]:
                self.notifier.notify_shift(shift, dry_run=self.dry_run)
            self._send_digest(len(new_matches) - alert_cap)
            log.info(
                "dry run — not clicking" if self.dry_run else "hold disabled — alert only"
            )
            return

        # You need to win ONE shift. Trying to hold a whole batch would race
        # itself, and every extra click is another chance to be flagged.
        hold_cap = max(1, int(self.cfg["hold"].get("max_per_poll", 1)))
        to_hold = new_matches[:hold_cap]

        for shift in to_hold:
            # In flight while the hold runs — not before it, not after. You
            # learn a shift appeared even if the hold then hangs, and the hold
            # never waits on Telegram.
            self.notify_async(self.notifier.notify_shift, shift, dry_run=False)

        log.info(
            "holding %s — %.0fms after poll start",
            to_hold[0].summary(), (time.perf_counter() - started) * 1000,
        )
        for shift in to_hold:
            self._hold(shift, poll_started=started)

        # Everything below is off the critical path: by now the shift is either
        # reserved or already gone.
        for shift in new_matches[len(to_hold):alert_cap]:
            self.notify_async(self.notifier.notify_shift, shift, dry_run=True)
        self._send_digest(len(new_matches) - alert_cap)

        if len(new_matches) > len(to_hold):
            log.info(
                "%d other match(es) alerted but not held (hold.max_per_poll=%d)",
                len(new_matches) - len(to_hold), hold_cap,
            )

    def _send_digest(self, held_back: int) -> None:
        """One digest instead of a hundred pings. Telegram rate-limits a single
        chat, so an unfiltered batch would arrive slowly, out of order, and
        bury the one shift that mattered."""
        if held_back <= 0:
            return
        log.info("%d further match(es) summarised rather than sent individually", held_back)
        self.notify_async(
            self.notifier.send_text,
            f"➕ <b>{held_back} more match(es)</b> this poll — "
            f"see the log, or tighten <code>filters</code> in config.yaml.",
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
    def _hold(self, shift, poll_started: float | None = None) -> None:
        missing = site_selectors.unconfigured_hold()
        if missing:
            # Bail before navigating: opening the listing and then failing on
            # the first click wastes the seconds that matter most, and lands
            # the browser on a job page the next poll has to navigate back off.
            log.error("cannot hold — unconfigured: %s", ", ".join(missing))
            self.notify_async(
                self.notifier.notify_error,
                "Matched a shift but cannot hold it — the apply-flow selectors "
                f"are still placeholders ({', '.join(missing)}). Open the "
                "listing yourself.",
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
            self.notify_async(
                self.notifier.notify_error, f"Could not open the listing: {exc}"
            )
            return

        SCREENSHOT_DIR.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        shot = SCREENSHOT_DIR / f"hold-{stamp}.png"

        # The fast route: if we can learn the scheduleId, the application can
        # be opened directly instead of clicked toward through five pages.
        direct = self._application_url(shift)
        if direct:
            log.info("holding via the direct application URL")
            result = site_selectors.hold_at_application(
                page,
                direct,
                stop_before_submit=self.cfg["hold"]["stop_before_submit"],
                timeout_ms=self.cfg["browser"]["action_timeout_ms"],
                screenshot_path=str(shot),
            )
            self._report_hold(shift, result, shot, poll_started)
            return

        result = site_selectors.hold_shift(
            page,
            shift,
            stop_before_submit=self.cfg["hold"]["stop_before_submit"],
            timeout_ms=self.cfg["browser"]["action_timeout_ms"],
            screenshot_path=str(shot),
        )

        self._report_hold(shift, result, shot, poll_started)

    def _application_url(self, shift) -> str | None:
        """The direct application URL for the best schedule on this job.

        Returns None whenever anything is missing, so the click path stays as
        the fallback rather than this becoming a new way to fail.
        """
        if not self.cfg["hold"].get("direct_apply", True):
            return None
        if self.api_client is None or not shift.id:
            return None

        try:
            found = self.api_client.fetch_schedules(shift.id)
        except Exception as exc:  # noqa: BLE001 - fall back to clicking
            log.warning("could not fetch schedules for %s: %s", shift.id, exc)
            return None

        open_slots = schedules_mod.bookable(found)
        if not open_slots:
            log.info(
                "%d schedule(s) for %s, none bookable — using the click path",
                len(found), shift.id,
            )
            return None

        # Most places left first: the likeliest to still be there in a race.
        open_slots.sort(key=lambda s: -(s.available or 1))
        best = open_slots[0]
        log.info(
            "%d bookable schedule(s); taking %s", len(open_slots), best.summary(),
        )
        return schedules_mod.application_url(
            self.cfg["site"]["base_url"], best.job_id or shift.id, best.id,
        )

    def _report_hold(self, shift, result, shot, poll_started=None) -> None:
        if result.timings:
            log.info("hold timings: %s", result.timing_summary())
        if poll_started is not None:
            # The number that decides whether you get the shift: how long from
            # the poll that spotted it to Amazon confirming the reservation.
            log.info(
                "detection -> %s in %.1fs",
                result.status, time.perf_counter() - poll_started,
            )

        if result.held:
            log.info("hold succeeded: %s", result.message)
            self.notify_async(
                self.notifier.notify_held,
                shift, self.cfg["hold"]["stop_before_submit"], detail=result.message,
            )
            self.notify_async(self.notifier.send_photo, shot, caption=result.message[:1000])
            return

        # Failed or uncertain both need a human, and quickly — an uncertain
        # hold may be a real reservation ticking down its three hours.
        log.error("hold %s: %s", result.status, result.message)
        urgency = (
            "⚠️ <b>CHECK THIS NOW</b>" if result.status == site_selectors.UNCERTAIN
            else "❌ <b>Hold failed</b>"
        )
        link = f"\n{result.url}" if result.url else ""
        self.notify_async(
            self.notifier.notify_error,
            f"{urgency}\n{shift.summary()}\n{result.message}{link}",
        )
        if shot.exists():
            self.notify_async(self.notifier.send_photo, shot, caption=result.message[:1000])


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
        try:
            return run_doctor(cfg)
        except browser_launch.ProfileInUse as exc:
            log.error("%s", exc)
            return 3

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

    try:
        return Watcher(cfg, live_override=args.live).run(once=args.once)
    except browser_launch.ProfileInUse as exc:
        # Two watchers on one profile is a configuration mistake, not a crash.
        log.error("%s", exc)
        return 3


if __name__ == "__main__":
    sys.exit(main())

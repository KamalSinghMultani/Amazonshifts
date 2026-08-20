"""Pre-live watcher: verified sessions + event-driven hold + latency records.

Inheritance remains deliberately additive:
    watcher.Watcher
      -> watcher_v2.ScheduleAwareWatcher
      -> watcher_v3.OptimizedWatcher
      -> watcher_v4.AutoSessionWatcher
      -> watcher_v5.PreLiveWatcher

No private candidate-application request is replayed here. The fast path still
uses Amazon's frontend and passively observes its reserve response.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import fast_hold
import hold_dom_probe
import hold_metrics
import schedules as schedules_mod
import site_selectors
import watcher as base
import watcher_v4
from shift_matcher import Shift

log = logging.getLogger("watcher")


class SessionBlockedResult(site_selectors.HoldResult):
    """A global session gate failure: keep the schedule retryable, stop the batch."""

    def worth_retrying(self) -> bool:
        return False


class PreLiveWatcher(watcher_v4.AutoSessionWatcher):
    """Final layer used for live watching and the real hold validation run."""

    def __init__(self, cfg: dict, live_override: bool = False) -> None:
        super().__init__(cfg, live_override=live_override)
        self.hold_page = None
        state_dir = Path(cfg["state"]["path"]).parent
        self.session_probe_input_path = state_dir / "session_probe_input_state.json"
        self._session_worker_force_login = False
        self.session_last_verified_monotonic: float | None = None
        self.session_last_verified_at: datetime | None = None
        self.session_status_reason = "application session has not been strongly verified yet"
        self._session_outage_alerted = False
        self._session_ready_announced = False
        self._session_block_alerted = False

        # If health workers ever stall or fail silently, do not trust a proof
        # forever. Normal 5-minute health checks renew this comfortably before
        # the lease expires. The real one-shot test marks its preflight session
        # verified explicitly in RealHoldTestWatcher.
        check_every = max(0.0, float(self.session_check_every or 0))
        self.session_verification_lease_seconds = max(600.0, check_every * 2.0)

    # ── session truth / gating ──────────────────────────────────────────────
    def _mark_session_verified(self, reason: str, *, notify: bool = True) -> None:
        was = self.session_ok
        self.session_ok = True
        self.session_last_verified_monotonic = time.monotonic()
        self.session_last_verified_at = datetime.now()
        self.session_status_reason = reason
        self.relogin_tried = False
        self._session_outage_alerted = False
        self._session_block_alerted = False

        # A recovered session is shared by every page in the context. Refresh
        # the dedicated application shell now, while no shift is being raced,
        # so the next direct route does not pay a cold frontend startup.
        if getattr(self, "context", None) is not None:
            self._prewarm_application_page(force=True)

        if was is False:
            log.info("LIVE HOLD SESSION RESTORED — holding re-armed (%s)", reason)
            if notify and self.alert_on_expiry:
                self.notify_async(
                    self.notifier.send_text,
                    "✅ <b>Amazon hold session restored</b>\n"
                    "Holding is re-armed; detection never stopped.",
                )
        elif not self._session_ready_announced:
            log.info("LIVE HOLD SESSION VERIFIED — reservations armed (%s)", reason)
            if notify and self.alert_on_expiry and not self.dry_run:
                self.notify_async(
                    self.notifier.send_text,
                    "✅ <b>Amazon hold session verified</b>\n"
                    "Reservations are armed.",
                )
            self._session_ready_announced = True

    def _verified_age_seconds(self) -> float | None:
        if self.session_last_verified_monotonic is None:
            return None
        return max(0.0, time.monotonic() - self.session_last_verified_monotonic)

    def _verified_age_text(self) -> str:
        age = self._verified_age_seconds()
        if age is None:
            return "never verified"
        if age < 60:
            return f"verified {age:.0f}s ago"
        return f"verified {age / 60:.1f} min ago"

    def _hold_session_ready(self) -> bool:
        if self.session_ok is not True:
            return False
        age = self._verified_age_seconds()
        if age is not None and age <= self.session_verification_lease_seconds:
            return True

        # A stale proof is not proof. Start a non-login health check and keep
        # any newly found schedule retryable until that check comes back.
        self.session_ok = None
        self.session_status_reason = "strong session proof became stale"
        log.warning(
            "hold session proof is stale (%s); holding temporarily gated until re-verified",
            self._verified_age_text(),
        )
        if self.session_worker is None and not self.holding:
            self._start_session_worker(force_login=False, reason="stale hold-session proof")
        return False

    def _mark_live_session_unhealthy(self, detail: str) -> None:
        was = self.session_ok
        self.session_ok = False
        self.session_status_reason = detail
        log.error("LIVE HOLD SESSION NOT VERIFIED — holding disabled: %s", detail)
        if self.alert_on_expiry and (was is not False or not self._session_outage_alerted):
            self.notify_async(
                self.notifier.notify_error,
                "🚨 <b>Amazon hold session is not verified</b>\n"
                "Holding is DISABLED so the watcher cannot silently miss a shift while signed out.\n"
                "Detection is still running and matching schedules stay retryable.\n"
                "Automatic recovery will be attempted if it is available.",
            )
            self._session_outage_alerted = True

    # ── isolated proof / recovery workers ──────────────────────────────────
    def _start_session_worker(self, *, force_login: bool, reason: str) -> bool:
        """Start either a prove-only worker or an explicit recovery worker.

        Health proof never logs in. A CAPTCHA/re-login block therefore cannot
        disable future health checks; it only prevents additional recovery
        attempts. This keeps live-session truth separate from login success.
        """
        if self.session_worker is not None or self.holding:
            return False
        self._roll_failed_relogin_day()

        if force_login:
            if self.relogin_blocked or not self._failure_budget_left():
                return False

        self.refresh_state_path.parent.mkdir(parents=True, exist_ok=True)
        for path in (self.refresh_state_path, self.refresh_result_path):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

        try:
            # Prove exactly what the live watcher is carrying right now rather
            # than an old auth_state.json snapshot.
            self.context.storage_state(path=str(self.session_probe_input_path))
        except Exception as exc:  # noqa: BLE001
            log.warning("could not snapshot live session for %s: %s", reason, exc)
            return False

        mode = "recover" if force_login else "prove"
        cmd = [
            sys.executable,
            str(Path(__file__).with_name("session_guard_worker.py")),
            "--config", self.config_path,
            "--input-state", str(self.session_probe_input_path),
            "--output-state", str(self.refresh_state_path),
            "--result", str(self.refresh_result_path),
            "--mode", mode,
        ]

        try:
            self.session_worker = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=str(Path(__file__).resolve().parent),
            )
            self.session_worker_reason = reason
            self._session_worker_force_login = force_login
            log.info("session %s started in background (%s)", mode, reason)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("could not start background session %s: %s", mode, exc)
            return False

    def _poll_session_worker(self) -> None:
        worker = self.session_worker
        if worker is None or worker.poll() is None:
            return

        reason = self.session_worker_reason or "session maintenance"
        forced = bool(self._session_worker_force_login)
        self.session_worker = None
        self.session_worker_reason = ""
        self._session_worker_force_login = False

        status = "error"
        detail = f"session worker exited {worker.returncode}"
        try:
            result = json.loads(self.refresh_result_path.read_text("utf-8"))
            status = str(result.get("status") or status)
            detail = str(result.get("detail") or detail)
        except Exception as exc:  # noqa: BLE001
            detail = f"could not read session worker result ({type(exc).__name__})"

        if status in ("ok", "healthy"):
            try:
                if self.refresh_state_path.exists():
                    self._apply_refreshed_state()
                self._mark_session_verified(
                    "fresh recovery strongly proved" if forced else "existing live session strongly proved"
                )
                log.info("background session %s succeeded (%s)", "recovery" if forced else "proof", detail)
                return
            except Exception as exc:  # noqa: BLE001
                status = "error"
                detail = f"verified state could not be imported ({type(exc).__name__})"

        if forced:
            # A replacement-login failure does NOT prove the currently running
            # session died. Preserve its live hold-ready state until an actual
            # prove-only health check says otherwise.
            self.failed_relogins_today += 1
            log.warning(
                "session recovery/refresh failed (%s): %s [failed today %d/%s]",
                status,
                detail,
                self.failed_relogins_today,
                self.max_failed_relogins_per_day or "unlimited",
            )
            if status == "captcha":
                self.relogin_blocked = True
                log.error("CAPTCHA blocked recovery; further login attempts paused, health proofs remain enabled")
            if not self._failure_budget_left():
                self.relogin_blocked = True
                log.error("failed-login budget exhausted; recovery paused until tomorrow")

            # Re-check the still-running live session soon. This proof never logs
            # in, so even a CAPTCHA-blocked recovery cannot blind session health.
            if self.session_check_every:
                self.next_session_check = min(
                    self.next_session_check or float("inf"),
                    time.monotonic() + 60.0,
                )

            if self.alert_on_expiry:
                if self.session_ok is True:
                    self.notify_async(
                        self.notifier.notify_error,
                        "⚠️ <b>Amazon session refresh failed</b>\n"
                        f"The current live hold session is still verified ({self._verified_age_text()}); "
                        "holding remains armed. A prove-only health check will run again soon."
                        + (
                            "\nAutomatic login recovery is paused because Amazon presented a challenge."
                            if status == "captcha" else ""
                        ),
                    )
                else:
                    self.notify_async(
                        self.notifier.notify_error,
                        "🚨 <b>Amazon session recovery failed</b>\n"
                        "Holding remains DISABLED; detection continues and schedules remain retryable."
                        + (
                            "\nAutomatic login recovery is paused because Amazon presented a challenge."
                            if status == "captcha" else ""
                        ),
                    )
            return

        # A prove-only failure is authoritative for the copied live session and
        # never attempted authentication. Gate holding immediately, then launch
        # one separate recovery attempt if allowed.
        self._mark_live_session_unhealthy(detail)
        if self.auto_relogin and not self.relogin_blocked and self._failure_budget_left():
            started = self._start_session_worker(
                force_login=True,
                reason=f"recovery after failed health proof ({reason})",
            )
            if started:
                log.warning("hold session proof failed; automatic recovery started")
        elif self.alert_on_expiry and self.relogin_blocked:
            self.notify_async(
                self.notifier.notify_error,
                "🚨 <b>Amazon hold session needs manual attention</b>\n"
                "Holding is disabled and automatic login recovery is paused. Detection is still running.",
            )

    def _start_api_mode(self, browser_cfg: dict) -> None:
        """Prime a persistent live profile from the latest saved verified state.

        Playwright ignores storage_state when launch_persistent_context is used.
        Without this, verify_session.py could prove/save a fresh application
        session while run_watcher.bat initially used older cookies/localStorage
        from browser_profile. Import the saved state into that live context
        before the polling loop starts; v4's background proof remains the
        authoritative verification/recovery layer afterwards.
        """
        saved_payload = None
        if browser_cfg.get("user_data_dir"):
            try:
                path = Path(self.cfg["browser"]["storage_state"])
                if path.exists():
                    saved_payload = json.loads(path.read_text("utf-8"))
                    cookies = saved_payload.get("cookies") if isinstance(saved_payload, dict) else None
                    if cookies:
                        self.context.add_cookies(cookies)
            except Exception as exc:  # noqa: BLE001
                log.debug("could not prime persistent cookies from saved state: %s", exc)
                saved_payload = None

        super()._start_api_mode(browser_cfg)

        if browser_cfg.get("user_data_dir") and isinstance(saved_payload, dict):
            expected_origin = watcher_v4._origin(self.cfg["site"]["base_url"])
            local_entries = []
            for item in saved_payload.get("origins") or []:
                if watcher_v4._origin(str(item.get("origin") or "")) == expected_origin:
                    local_entries.extend(item.get("localStorage") or [])

            try:
                if self.page is not None and local_entries:
                    self.page.evaluate(
                        """entries => {
                            for (const item of entries) {
                                if (item && typeof item.name === 'string') {
                                    localStorage.setItem(item.name, item.value ?? '');
                                }
                            }
                        }""",
                        local_entries,
                    )
                    if self.token_source is not None:
                        self.token_source.refresh()
                log.info(
                    "primed persistent live context from saved verified state: %d localStorage item(s)",
                    len(local_entries),
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("saved session cookies loaded but localStorage priming failed: %s", exc)

        self._prewarm_application_page()

    def _prewarm_application_page(self, *, force: bool = False) -> bool:
        """Start Amazon's application frontend before a schedule is detected.

        This performs only ordinary page navigation. It has no job or schedule
        identifiers and never clicks Create Application, Integrity, or later
        controls. The token-minting job-search page remains separate.
        """
        if not self.cfg.get("hold", {}).get("prewarm_application", True):
            return False
        if getattr(self, "context", None) is None:
            return False

        page = self.hold_page
        try:
            closed = page is None or page.is_closed()
        except Exception:  # noqa: BLE001
            closed = True
        if closed:
            try:
                page = self.context.new_page()
                self.hold_page = page
            except Exception as exc:  # noqa: BLE001
                log.warning("could not create application prewarm page: %s", type(exc).__name__)
                return False
        elif not force:
            return True

        target = f"{self.cfg['site']['base_url'].rstrip('/')}/application/ca/"
        try:
            began = time.perf_counter()
            page.goto(
                target,
                wait_until="commit",
                timeout=min(int(self.cfg["browser"]["nav_timeout_ms"]), 10000),
            )
            log.info(
                "application frontend prewarm started in %.0fms",
                (time.perf_counter() - began) * 1000,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            # Prewarm is an optimization, never session proof and never a reason
            # to disable detection. The direct route still gets its normal try.
            log.warning("application frontend prewarm could not start: %s", type(exc).__name__)
            return False

    def _record_hold_metric(
        self,
        shift: Shift,
        result,
        *,
        poll_started: float | None,
        dispatch_started: float,
        backend_detail: str = "",
    ) -> None:
        raw = shift.raw or {}
        now = time.perf_counter()
        try:
            hold_metrics.append_record(
                job_id=raw.get("jobId") or raw.get("parentJobId") or shift.id,
                schedule_id=raw.get("scheduleId"),
                title=shift.title,
                location=shift.location,
                status=getattr(result, "status", "unknown"),
                message=getattr(result, "message", ""),
                poll_to_dispatch_ms=(
                    (dispatch_started - poll_started) * 1000
                    if poll_started is not None else None
                ),
                total_from_poll_ms=(
                    (now - poll_started) * 1000 if poll_started is not None else None
                ),
                hold_timings=getattr(result, "timings", ()) or (),
                backend_detail=backend_detail,
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("could not append hold timing record: %s", exc)

    def _direct_hold(self, shift: Shift, poll_started: float | None = None):
        raw = shift.raw or {}
        schedule_id = raw.get("scheduleId")
        job_id = raw.get("jobId") or raw.get("parentJobId")
        if not (schedule_id and job_id and self.cfg["hold"].get("direct_apply", True)):
            return None

        page = self.hold_page
        try:
            if page is not None and page.is_closed():
                page = None
        except Exception:  # noqa: BLE001
            page = None
        if page is None:
            page = self.page
        if page is None:
            page = self.context.new_page()
        self.hold_page = page

        base.SCREENSHOT_DIR.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
        shot = base.SCREENSHOT_DIR / f"hold-{stamp}.png"
        direct = schedules_mod.application_url(
            self.cfg["site"]["base_url"], str(job_id), str(schedule_id)
        )

        dispatch_started = time.perf_counter()
        if poll_started is not None:
            log.info(
                "FAST HOLD dispatch schedule=%s at %.0fms from poll start",
                schedule_id,
                (dispatch_started - poll_started) * 1000,
            )
        else:
            log.info("FAST HOLD dispatch schedule=%s", schedule_id)

        probe = hold_dom_probe.HoldDomProbe(page)
        probe.start()
        result = None
        backend_detail = ""
        try:
            result, backend_detail = fast_hold.hold(
                page,
                direct,
                str(schedule_id),
                base_url=self.cfg["site"]["base_url"],
                stop_before_submit=self.cfg["hold"]["stop_before_submit"],
                timeout_ms=self.cfg["browser"]["action_timeout_ms"],
                screenshot_path=str(shot),
                manual_integrity_wait=bool(
                    self.cfg["hold"].get("manual_integrity_wait", False)
                ),
                manual_integrity_timeout_ms=int(
                    self.cfg["hold"].get("manual_integrity_timeout_ms", 120000)
                ),
                auto_integrity_agree=bool(
                    self.cfg["hold"].get("auto_integrity_agree", False)
                ),
            )
        finally:
            if result is not None:
                probe.annotate(result)
            else:
                probe.stop()

        self._record_hold_metric(
            shift,
            result,
            poll_started=poll_started,
            dispatch_started=dispatch_started,
            backend_detail=backend_detail,
        )

        low_message = (getattr(result, "message", "") or "").lower()
        if "redirected to login" in low_message or site_selectors.is_login_page(page):
            # This is direct evidence from the actual reservation path, stronger
            # than any background helper. Stop wasting the batch on compatibility
            # fallbacks and immediately move the watcher into recovery mode.
            self._mark_live_session_unhealthy("actual hold path redirected to login")
            if self.auto_relogin and self.session_worker is None and not self.relogin_blocked:
                self._start_session_worker(
                    force_login=True,
                    reason="actual hold path proved session dead",
                )
            self._report_hold(shift, result, shot, poll_started)
            return result

        integrity_attempted = any(
            name == "integrity agree clicked"
            for name, _ms in (getattr(result, "timings", ()) or ())
        )
        if (
            result.held
            or result.status in (
                site_selectors.UNCERTAIN,
                site_selectors.IDENTITY_VERIFICATION_REQUIRED,
            )
            or integrity_attempted
        ):
            if integrity_attempted and result.status == site_selectors.FAILED:
                log.info("integrity reservation attempt finished without a reserve; skipping compatibility fallback")
            self._report_hold(shift, result, shot, poll_started)
            return result

        if not self.cfg.get("hold", {}).get("compatibility_fallback", False):
            log.warning(
                "fast direct route failed (%s); compatibility fallback disabled for latency",
                (result.message or "")[:140],
            )
            self._report_hold(shift, result, shot, poll_started)
            return result

        log.warning(
            "fast direct route failed (%s); trying original click fallback",
            (result.message or "")[:140],
        )
        parent = Shift(
            id=str(job_id),
            title=shift.title,
            location=shift.location,
            schedule=shift.schedule,
            pay_rate=shift.pay_rate,
            url=f"{self.cfg['site']['base_url'].rstrip('/')}/app#/jobDetail?jobId={job_id}",
            raw={"jobId": str(job_id)},
        )
        original = self.cfg["hold"].get("direct_apply", True)
        self.cfg["hold"]["direct_apply"] = False
        fallback_started = time.perf_counter()
        try:
            fallback = super()._hold(parent, poll_started=poll_started)
        finally:
            self.cfg["hold"]["direct_apply"] = original

        if fallback is not None:
            fallback_shift = Shift(
                id=shift.id,
                title=shift.title,
                location=shift.location,
                schedule=shift.schedule,
                pay_rate=shift.pay_rate,
                raw=dict(raw),
            )
            self._record_hold_metric(
                fallback_shift,
                fallback,
                poll_started=poll_started,
                dispatch_started=fallback_started,
                backend_detail="compatibility fallback",
            )
        return fallback

    def _hold(self, shift, poll_started: float | None = None):
        if not self._hold_session_ready():
            if not self._session_block_alerted and self.alert_on_expiry:
                self.notify_async(
                    self.notifier.notify_error,
                    "⚠️ <b>Shift found, but holding is temporarily gated</b>\n"
                    "The application session is not currently strongly verified. "
                    "The schedule remains retryable while verification/recovery runs.",
                )
                self._session_block_alerted = True
            if self.session_worker is None and not self.holding:
                self._start_session_worker(force_login=False, reason="shift found while hold session unverified")
            return SessionBlockedResult(
                site_selectors.FAILED,
                "holding blocked: application session is not currently verified; schedule remains retryable",
                url=getattr(self.page, "url", "") if self.page is not None else "",
            )
        return super()._hold(shift, poll_started=poll_started)

    def _stop_session_worker(self) -> None:
        worker = getattr(self, "session_worker", None)
        if worker is None or worker.poll() is not None:
            return
        try:
            worker.terminate()
            worker.wait(timeout=3)
        except Exception:
            try:
                worker.kill()
            except Exception:
                pass
        finally:
            self.session_worker = None

    def run(self, once: bool = False) -> int:
        try:
            return super().run(once=once)
        finally:
            self._stop_session_worker()


base.Watcher = PreLiveWatcher


if __name__ == "__main__":
    raise SystemExit(base.main())

"""Final reliability guard for the live Amazon shift watcher.

v5 separates health proof from login recovery and gates every hold on a recent
strong application-session proof. v6 tightens classification further:

* live holding always gets a startup proof, even if auto-relogin is disabled;
* only a definite redirect to the login flow marks the live session expired;
* inconclusive network/WAF/React health probes never trigger a login attempt;
* inconclusive probes are retried soon and the verification lease prevents a
  stale proof from being trusted forever;
* schedules found while holding is gated remain retryable;
* a login failure discovered during the hold fence queues recovery immediately
  after that fence is released instead of trying to launch it mid-click.
"""

from __future__ import annotations

import json
import logging
import time

import site_selectors
import watcher as base
import watcher_v3
import watcher_v5

log = logging.getLogger("watcher")


class HoldReadyWatcher(watcher_v5.PreLiveWatcher):
    """v5 with fail-closed hold readiness and conservative proof classification."""

    def __init__(self, cfg: dict, live_override: bool = False) -> None:
        super().__init__(cfg, live_override=live_override)
        self._recovery_due_after_hold = False

    def _loop(self, once: bool = False) -> None:
        # Session health is a prerequisite for live holding, not an auto-login
        # feature. Always start a harmless prove-only worker for a normal live
        # watcher. Recovery remains controlled separately by auto_relogin.
        #
        # A caller such as real_hold_test may already mark the exact preflight
        # state verified before run(); in that case do not prove it twice.
        live_holder = (
            not once
            and not self.dry_run
            and bool((self.cfg.get("hold") or {}).get("enabled"))
        )
        if live_holder and self.session_ok is not True:
            if self._start_session_worker(
                force_login=False,
                reason="startup strong hold-session proof",
            ):
                now = time.monotonic()
                if self.session_check_every:
                    self.next_session_check = now + self.session_check_every
                log.info(
                    "startup protected-session proof running in background; "
                    "detection continues, holding stays gated until proof succeeds"
                )

        # Skip v4's older startup hook (which tied proof to auto_relogin) and
        # enter v3's non-blocking detection/maintenance loop directly. Dynamic
        # dispatch still calls all v6/v5 overrides below.
        watcher_v3.OptimizedWatcher._loop(self, once=once)

    def _hold_session_ready(self) -> bool:
        if self.session_ok is not True:
            if self.session_check_every:
                self.next_session_check = 0.0
            return False

        age = self._verified_age_seconds()
        if age is not None and age <= self.session_verification_lease_seconds:
            return True

        self.session_ok = None
        self.session_status_reason = "strong application-session proof became stale"
        if self.session_check_every:
            self.next_session_check = 0.0
        log.warning(
            "LIVE HOLD SESSION proof stale (%s) — holding gated until re-verified",
            self._verified_age_text(),
        )
        if self.session_worker is None and not self.holding:
            self._start_session_worker(force_login=False, reason="stale hold-session proof")
        return False

    def _hold(self, shift, poll_started: float | None = None):
        status_before = self.session_ok
        if not self._hold_session_ready():
            if not self._session_block_alerted and self.alert_on_expiry:
                if status_before is False:
                    text = (
                        "🚨 <b>Amazon hold session is dead / signed out</b>\n"
                        "A shift was found, but no hold was attempted because the protected "
                        "application session needs a login. Detection continues and the "
                        "schedule remains retryable while recovery runs."
                    )
                else:
                    text = (
                        "⚠️ <b>Shift found, but holding is temporarily gated</b>\n"
                        "The protected application session is awaiting a strong verification. "
                        "Detection continues and the schedule remains retryable."
                    )
                self.notify_async(self.notifier.notify_error, text)
                self._session_block_alerted = True

            if status_before is False and self.auto_relogin and not self.relogin_blocked:
                # v3 holds self.holding=True around this call, so starting a
                # worker here would be rejected by the hold fence. Queue it for
                # session_maintenance_tick immediately after the poll.
                self._recovery_due_after_hold = True
            elif self.session_check_every:
                self.next_session_check = 0.0

            return watcher_v5.SessionBlockedResult(
                site_selectors.FAILED,
                "session is dead or not verified; holding blocked and schedule remains retryable",
                url=getattr(self.page, "url", "") if self.page is not None else "",
            )

        result = super()._hold(shift, poll_started=poll_started)
        low = (getattr(result, "message", "") or "").lower()
        if "redirected to login" in low or "needs a login" in low:
            self._recovery_due_after_hold = bool(self.auto_relogin and not self.relogin_blocked)
        return result

    def session_maintenance_tick(self) -> None:
        # First consume any worker that just finished.
        self._poll_session_worker()
        if self.session_worker is not None or self.holding or self.dry_run:
            return

        if self._recovery_due_after_hold:
            self._recovery_due_after_hold = False
            if self.auto_relogin and not self.relogin_blocked and self._failure_budget_left():
                if self._start_session_worker(
                    force_login=True,
                    reason="recovery queued by hold-path session failure",
                ):
                    log.warning("hold-path session failure: recovery started immediately after hold fence")
                    return

        # Normal periodic health / proactive refresh scheduling from v3.
        super().session_maintenance_tick()

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
        definitive_expiry = False
        try:
            result = json.loads(self.refresh_result_path.read_text("utf-8"))
            status = str(result.get("status") or status)
            detail = str(result.get("detail") or detail)
            definitive_expiry = bool(result.get("definitive_expiry", False))
        except Exception as exc:  # noqa: BLE001
            detail = f"could not read session worker result ({type(exc).__name__})"

        if status in ("ok", "healthy"):
            try:
                if self.refresh_state_path.exists():
                    self._apply_refreshed_state()
                self._mark_session_verified(
                    "fresh recovery strongly proved" if forced
                    else "existing live session strongly proved"
                )
                log.info(
                    "background session %s succeeded (%s)",
                    "recovery" if forced else "proof",
                    detail,
                )
                return
            except Exception as exc:  # noqa: BLE001
                status = "error"
                detail = f"verified state could not be imported ({type(exc).__name__})"
                definitive_expiry = False

        if forced:
            # A failed replacement login says nothing about whether the existing
            # live browser session is still usable. Preserve session_ok exactly
            # as it was and re-prove that live state soon.
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
                log.error(
                    "CAPTCHA blocked login recovery; future recovery attempts paused, "
                    "prove-only health checks remain enabled"
                )
            if not self._failure_budget_left():
                self.relogin_blocked = True
                log.error("failed-login budget exhausted; recovery paused until tomorrow")

            if self.session_check_every:
                self.next_session_check = time.monotonic() + 60.0

            if self.alert_on_expiry:
                if self.session_ok is True:
                    self.notify_async(
                        self.notifier.notify_error,
                        "⚠️ <b>Amazon session refresh failed</b>\n"
                        f"Your current live hold session is still verified ({self._verified_age_text()}); "
                        "holding remains ARMED. A prove-only health check will run again within about a minute."
                        + (
                            "\nAutomatic login recovery is paused because Amazon presented a challenge."
                            if status == "captcha" else ""
                        ),
                    )
                else:
                    self.notify_async(
                        self.notifier.notify_error,
                        "🚨 <b>Amazon session recovery failed</b>\n"
                        "Holding remains DISABLED; detection continues and matching schedules remain retryable."
                        + (
                            "\nAutomatic login recovery is paused because Amazon presented a challenge."
                            if status == "captcha" else ""
                        ),
                    )
            return

        if not definitive_expiry:
            # Slow protected page, WAF response, browser launch issue, etc. This
            # is not evidence that the account logged out. Do not authenticate.
            log.warning(
                "session health proof inconclusive (%s): %s — no login attempted",
                status,
                detail,
            )
            if self.session_check_every:
                self.next_session_check = time.monotonic() + 60.0
            if self.alert_on_expiry:
                if self.session_ok is True:
                    self.notify_async(
                        self.notifier.send_text,
                        "⚠️ <b>Amazon session health check was inconclusive</b>\n"
                        f"Holding remains armed under the last strong proof ({self._verified_age_text()}). "
                        "No login was attempted; health will be checked again soon.",
                    )
                elif not self._session_block_alerted:
                    self.notify_async(
                        self.notifier.notify_error,
                        "⚠️ <b>Amazon hold session is awaiting verification</b>\n"
                        "Holding stays gated until a strong protected-session proof succeeds. "
                        "Detection continues and schedules remain retryable. No login was attempted.",
                    )
                    self._session_block_alerted = True
            return

        # A prove-only worker copied from the live browser was actually
        # redirected to login. This is authoritative session-expiry evidence.
        self._mark_live_session_unhealthy(detail)
        if self.auto_relogin and not self.relogin_blocked and self._failure_budget_left():
            if self._start_session_worker(
                force_login=True,
                reason=f"recovery after definite live-session expiry ({reason})",
            ):
                log.warning("live hold session expired; automatic recovery started")
        elif self.alert_on_expiry:
            self.notify_async(
                self.notifier.notify_error,
                "🚨 <b>Amazon hold session needs manual attention</b>\n"
                "The protected application session redirected to login. Holding is disabled; "
                "detection continues and schedules remain retryable.",
            )


base.Watcher = HoldReadyWatcher


if __name__ == "__main__":
    raise SystemExit(base.main())

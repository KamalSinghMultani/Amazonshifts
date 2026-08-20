"""Latency-first browser-driven hold path.

This deliberately does NOT replay Amazon's candidate-application APIs. It
navigates the same application URL and lets Amazon's own frontend issue the
create/update calls. The only backend interaction here is passive response
observation via hold_verify.SoftReserveObserver.
"""

from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import failure_capture
import hold_verify
import site_selectors


log = logging.getLogger("watcher")

NEXT = "[data-test-id='layout'] button:has-text('Next')"
CREATE = "[data-test-id='layout'] button:has-text('Create Application')"
INTEGRITY_AGREE = "[data-test-id='integrity-notice-agree-button']"
INTEGRITY_ROUTE = "application-integrity-notice"
HOLD_TEXT = "holding a spot"
UNAVAILABLE_TEXT_PATTERNS = (
    "shift is no longer available",
    "shift is not available",
    "shift not available anymore",
    "this shift is no longer available",
    "schedule is no longer available",
    "schedule is not available",
    "this schedule is no longer available",
    "selected shift is no longer available",
    "selected schedule is no longer available",
)
IDENTITY_TEXT_PATTERNS = (
    "let's confirm it's you",
    "let’s confirm it’s you",
    "start identity verification",
)


def _visible(page: Any, selector: str) -> bool:
    try:
        item = page.locator(selector).first
        return item.count() > 0 and item.is_visible()
    except Exception:
        return False


def _enabled(page: Any, selector: str) -> bool:
    try:
        item = page.locator(selector).first
        return item.count() > 0 and item.is_visible() and item.is_enabled()
    except Exception:
        return False


def _body_text(page: Any) -> str:
    try:
        return " ".join((page.inner_text("body") or "").split())
    except Exception:
        return ""


def _banner(page: Any) -> str:
    text = _body_text(page)
    low = text.lower()
    idx = low.find(HOLD_TEXT)
    if idx < 0:
        return ""
    return text[idx:idx + 220]


def _availability_failure(page: Any) -> str:
    """Return a narrow visible unavailable message, or an empty string."""
    text = _body_text(page)
    low = text.lower()
    for pattern in UNAVAILABLE_TEXT_PATTERNS:
        idx = low.find(pattern)
        if idx >= 0:
            start = max(0, idx - 80)
            end = min(len(text), idx + len(pattern) + 160)
            return text[start:end]
    return ""


def _integrity_notice(page: Any) -> bool:
    """Whether Amazon advanced to its application-integrity notice."""
    try:
        if INTEGRITY_ROUTE in (getattr(page, "url", "") or "").lower():
            return True
    except Exception:
        pass
    return _visible(page, INTEGRITY_AGREE)


def _identity_verification_required(page: Any) -> bool:
    """Detect the eKYC handoff without reading or exposing its identifiers."""
    try:
        url = (getattr(page, "url", "") or "").lower()
        if "/liveness-check" in url or "remotekyc" in url:
            return True
    except Exception:
        pass
    text = _body_text(page).lower()
    return any(pattern in text for pattern in IDENTITY_TEXT_PATTERNS)


def _safe_result_url(page: Any, fallback: str = "") -> str:
    """Strip KYC query/fragment values before a result can be logged/notified."""
    raw = str(getattr(page, "url", "") or fallback or "")
    low = raw.lower()
    if "/liveness-check" not in low and "remotekyc" not in low:
        return raw
    try:
        parsed = urlsplit(raw)
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    except Exception:
        return ""


def hold(
    page: Any,
    application_url: str,
    expected_schedule_id: str,
    *,
    base_url: str,
    stop_before_submit: bool,
    timeout_ms: int,
    screenshot_path: str | None = None,
    manual_integrity_wait: bool = False,
    manual_integrity_timeout_ms: int = 120000,
    auto_integrity_agree: bool = False,
) -> tuple[site_selectors.HoldResult, str]:
    """Return (HoldResult, backend_detail) with minimal local waiting.

    Polling is 50ms and readiness-driven. After Create Application is pressed,
    return immediately when Amazon's own update-application response proves
    JOB_SELECTED + expected schedule + reserve expiry.

    Integrity handling has three modes:
      * default: stop at the notice and report UNCERTAIN;
      * manual_integrity_wait: keep the observer alive while the user clicks;
      * auto_integrity_agree: click only the integrity notice's I Agree button,
        then observe reserve/unavailable state and stop before later forms.

    The auto mode is deliberately opt-in. It does not fill personal details,
    documents, assessments, identity checks, or any later application fields.
    """
    began = time.perf_counter()
    timings: list[tuple[str, float]] = []

    def mark(label: str) -> None:
        timings.append((label, (time.perf_counter() - began) * 1000))

    observer = hold_verify.SoftReserveObserver(page, expected_schedule_id)
    with observer:
        try:
            page.goto(application_url, wait_until="commit", timeout=max(timeout_ms, 20000))
            mark("navigation committed")
        except Exception as exc:  # noqa: BLE001
            captured = failure_capture.capture(
                page,
                getattr(page, "context", None),
                base_url,
                "hold-navigation",
                extra={"error": str(exc)[:200]},
            )
            return (
                site_selectors.HoldResult(
                    site_selectors.FAILED,
                    f"could not open application: {str(exc)[:150]}; screenshot: {captured.get('screenshot') or '<none>'}",
                    url=getattr(page, "url", "") or application_url,
                    timings=timings,
                ),
                observer.detail(),
            )

        deadline = time.perf_counter() + max(timeout_ms, 20000) / 1000.0
        next_clicked = False
        create_clicked = False
        overlay_checked = False
        integrity_seen = False
        integrity_left = False
        integrity_agree_clicked = False
        create_visible_seen = False
        create_enabled_seen = False
        create_actionable_seen = False
        agree_visible_seen = False
        agree_enabled_seen = False
        agree_actionable_seen = False
        observer_update_marked = False
        integrity_overlay_checked = False
        integrity_click_error = ""

        while time.perf_counter() < deadline:
            if observer.confirmed:
                mark("backend reserve confirmed")
                if screenshot_path:
                    try:
                        page.screenshot(path=screenshot_path, full_page=False)
                    except Exception:
                        pass
                return (
                    site_selectors.HoldResult(
                        site_selectors.CONFIRMED,
                        f"SPOT HELD — {observer.detail()}",
                        url=getattr(page, "url", "") or application_url,
                        timings=timings,
                    ),
                    observer.detail(),
                )

            if _identity_verification_required(page):
                mark("identity verification required")
                return (
                    site_selectors.HoldResult(
                        site_selectors.IDENTITY_VERIFICATION_REQUIRED,
                        "Amazon requires identity verification for this candidate account. "
                        "Application automation stopped before consent, selfie, ID upload, or submission; "
                        "complete the verification manually on Amazon if you choose.",
                        url=_safe_result_url(page, application_url),
                        timings=timings,
                    ),
                    observer.detail(),
                )

            if site_selectors.is_login_page(page):
                captured = failure_capture.capture(
                    page,
                    getattr(page, "context", None),
                    base_url,
                    "hold-login-redirect",
                )
                return (
                    site_selectors.HoldResult(
                        site_selectors.FAILED,
                        f"application redirected to login; screenshot: {captured.get('screenshot') or '<none>'}",
                        url=getattr(page, "url", ""),
                        timings=timings,
                    ),
                    observer.detail(),
                )

            # Overlay work is deferred until an actionable application control
            # exists. This avoids modal-probe cost while React is still mounting.
            next_ready = _enabled(page, NEXT)
            create_visible = _visible(page, CREATE)
            if create_visible and not create_visible_seen:
                create_visible_seen = True
                mark("create application visible")
            create_ready = _enabled(page, CREATE)
            if create_ready and not create_enabled_seen:
                create_enabled_seen = True
                mark("create application enabled")
            if (next_ready or create_ready) and not overlay_checked:
                mark("application action ready")
                site_selectors.dismiss_overlays(
                    page, timeout_ms=min(timeout_ms, 1500), rounds=2
                )
                overlay_checked = True
                next_ready = _enabled(page, NEXT)
                create_ready = _enabled(page, CREATE)

            if create_ready and overlay_checked and not create_actionable_seen:
                create_actionable_seen = True
                mark("create application actionable")

            if not next_clicked and next_ready:
                try:
                    page.locator(NEXT).first.click(timeout=min(timeout_ms, 2000))
                    next_clicked = True
                    overlay_checked = False
                    mark("next clicked")
                    page.wait_for_timeout(25)
                    continue
                except Exception:
                    pass

            if create_ready and not create_clicked:
                # Critical-path rule: never screenshot before the committing
                # click. The 2026-08-19 live run spent ~723ms between button
                # ready and click while a diagnostic screenshot was taken.
                mark("create application ready")
                if stop_before_submit:
                    return (
                        site_selectors.HoldResult(
                            site_selectors.FAILED,
                            "stopped before Create Application by configuration",
                            url=getattr(page, "url", ""),
                            timings=timings,
                        ),
                        observer.detail(),
                    )
                try:
                    page.locator(CREATE).first.click(timeout=min(timeout_ms, 2000))
                    create_clicked = True
                    mark("create application clicked")
                    # Give Playwright an event-loop turn so response handlers can
                    # consume an already-fast backend response immediately.
                    page.wait_for_timeout(10)
                    continue
                except Exception as exc:  # noqa: BLE001
                    captured = failure_capture.capture(
                        page,
                        getattr(page, "context", None),
                        base_url,
                        "hold-create-click",
                        extra={"error": str(exc)[:200]},
                    )
                    return (
                        site_selectors.HoldResult(
                            site_selectors.FAILED,
                            f"could not press Create Application: {str(exc)[:120]}; screenshot: {captured.get('screenshot') or '<none>'}",
                            url=getattr(page, "url", ""),
                            timings=timings,
                        ),
                        observer.detail(),
                    )

            if create_clicked:
                on_integrity = _integrity_notice(page)
                if on_integrity and not integrity_seen:
                    integrity_seen = True
                    mark("integrity notice reached")

                    if not auto_integrity_agree and not manual_integrity_wait:
                        captured = failure_capture.capture(
                            page,
                            getattr(page, "context", None),
                            base_url,
                            "hold-integrity-notice",
                            extra={"reserve_confirmed": False},
                        )
                        return (
                            site_selectors.HoldResult(
                                site_selectors.UNCERTAIN,
                                "Create Application advanced to Amazon's Application Integrity Notice, "
                                "but the shift reserve was not confirmed. Manual applicant action is "
                                "required; automation stopped without clicking I Agree. "
                                f"Screenshot: {captured.get('screenshot') or '<none>'}",
                                url=getattr(page, "url", ""),
                                timings=timings,
                            ),
                            observer.detail(),
                        )

                    if manual_integrity_wait and not auto_integrity_agree:
                        # Manual test-only handoff. Keep the same response
                        # observer attached while the applicant acts.
                        deadline = max(
                            deadline,
                            time.perf_counter()
                            + max(1000, int(manual_integrity_timeout_ms)) / 1000.0,
                        )
                        log.warning(
                            "MANUAL ACTION REQUIRED — Amazon's Application Integrity Notice is open. "
                            "Click I Agree yourself in the visible browser. The watcher will only "
                            "observe what Amazon does next."
                        )

                if on_integrity:
                    agree_visible = _visible(page, INTEGRITY_AGREE)
                    if agree_visible and not agree_visible_seen:
                        agree_visible_seen = True
                        mark("integrity agree visible")
                    agree_enabled = _enabled(page, INTEGRITY_AGREE)
                    if agree_enabled and not agree_enabled_seen:
                        agree_enabled_seen = True
                        mark("integrity agree enabled")

                    # Do no blocking wait from the route marker: the route can
                    # arrive before React inserts/enables the real button.
                    if auto_integrity_agree and agree_enabled and not integrity_agree_clicked:
                        if not integrity_overlay_checked:
                            site_selectors.dismiss_overlays(
                                page, timeout_ms=min(timeout_ms, 1500), rounds=2
                            )
                            integrity_overlay_checked = True
                            agree_enabled = _enabled(page, INTEGRITY_AGREE)
                        if agree_enabled:
                            if not agree_actionable_seen:
                                agree_actionable_seen = True
                                mark("integrity agree actionable")
                            try:
                                page.locator(INTEGRITY_AGREE).first.click(
                                    timeout=min(timeout_ms, 2000)
                                )
                                integrity_agree_clicked = True
                                mark("integrity agree clicked")
                                # Preserve the conservative full window when no
                                # passive response or terminal page evidence is
                                # observed. Evidence below can end it earlier.
                                deadline = max(
                                    deadline,
                                    time.perf_counter()
                                    + max(timeout_ms, 20000) / 1000.0,
                                )
                                log.info(
                                    "integrity I Agree clicked; observing reserve result only — later application fields will not be touched"
                                )
                                page.wait_for_timeout(10)
                                continue
                            except Exception as exc:  # noqa: BLE001
                                # A transient overlay/layout transition is not a
                                # terminal click failure. Keep polling the same
                                # normal browser control until the deadline.
                                integrity_click_error = type(exc).__name__

                if integrity_seen and not integrity_left and not on_integrity:
                    integrity_left = True
                    mark(
                        "integrity notice left after auto agree"
                        if integrity_agree_clicked
                        else "integrity notice left manually"
                    )
                    # Once the integrity step has completed, give Amazon a fresh
                    # backend confirmation window rather than consuming the
                    # original application-load deadline.
                    deadline = max(
                        deadline,
                        time.perf_counter() + max(timeout_ms, 20000) / 1000.0,
                    )
                    log.info(
                        "integrity step left; waiting for reserve confirmation, unavailable result, or next page"
                    )

                # After I Agree, an unavailable message is terminal and is
                # intentionally distinct from an unproven timeout.
                if integrity_agree_clicked or integrity_left:
                    if observer.relevant_update_seen and not observer_update_marked:
                        observer_update_marked = True
                        mark("post integrity update response observed")
                    unavailable = _availability_failure(page)
                    if unavailable:
                        mark("schedule unavailable after integrity")
                        captured = failure_capture.capture(
                            page,
                            getattr(page, "context", None),
                            base_url,
                            "hold-schedule-unavailable-after-integrity",
                            extra={"reserve_confirmed": False},
                        )
                        return (
                            site_selectors.HoldResult(
                                site_selectors.FAILED,
                                "Schedule was no longer available after the integrity step; "
                                f"no reserve was confirmed. Screenshot: {captured.get('screenshot') or '<none>'}",
                                url=getattr(page, "url", ""),
                                timings=timings,
                            ),
                            observer.detail(),
                        )

                    if observer.settled_without_confirmation():
                        mark("post integrity update lacked reserve proof")
                        captured = failure_capture.capture(
                            page,
                            getattr(page, "context", None),
                            base_url,
                            "hold-post-integrity-update-uncertain",
                            extra={
                                "reserve_confirmed": False,
                                "expected_schedule_response_seen": True,
                            },
                        )
                        return (
                            site_selectors.HoldResult(
                                site_selectors.UNCERTAIN,
                                "Amazon's own application update for the expected schedule was observed, "
                                "but it did not contain complete reserve proof (JOB_SELECTED + matching "
                                "scheduleId + soft reserve expiration). Automation stopped before later "
                                f"application fields. Screenshot: {captured.get('screenshot') or '<none>'}",
                                url=getattr(page, "url", ""),
                                timings=timings,
                            ),
                            observer.detail(),
                        )

                banner = _banner(page)
                if banner:
                    mark("holding banner seen")
                    return (
                        site_selectors.HoldResult(
                            site_selectors.CONFIRMED,
                            f"SPOT HELD — {banner}",
                            url=getattr(page, "url", ""),
                            banner=banner,
                            timings=timings,
                        ),
                        observer.detail(),
                    )

            try:
                page.wait_for_timeout(50)
            except Exception:
                break

    if integrity_seen and (manual_integrity_wait or auto_integrity_agree):
        if auto_integrity_agree:
            if integrity_agree_clicked:
                mark("post integrity confirmation timeout")
                category = "hold-after-auto-integrity-no-confirmation"
                message = (
                    "I Agree was clicked, but neither a verified reserve nor an unavailable result "
                    "was observed. Automation stopped before touching any later application fields."
                )
            else:
                mark("integrity agree actionability timeout")
                category = "hold-integrity-agree-not-actionable"
                message = (
                    "Amazon's Integrity Notice appeared, but its I Agree control never became "
                    "actionable before the timeout; no reserve was confirmed."
                )
        else:
            category = (
                "hold-after-manual-integrity-no-confirmation"
                if integrity_left
                else "hold-manual-integrity-timeout"
            )
            message = (
                "The Application Integrity Notice was left manually, but no reserve confirmation "
                "was observed. Check the current page and screenshot."
                if integrity_left
                else "Timed out waiting for the applicant to complete the Application Integrity Notice."
            )
        captured = failure_capture.capture(
            page,
            getattr(page, "context", None),
            base_url,
            category,
            extra={
                "reserve_confirmed": False,
                "integrity_left": integrity_left,
                "integrity_agree_clicked": integrity_agree_clicked,
                "integrity_click_error_type": integrity_click_error,
            },
        )
        return (
            site_selectors.HoldResult(
                site_selectors.UNCERTAIN,
                f"{message} Screenshot: {captured.get('screenshot') or '<none>'}",
                url=getattr(page, "url", ""),
                timings=timings,
            ),
            observer.detail(),
        )

    captured = failure_capture.capture(
        page,
        getattr(page, "context", None),
        base_url,
        "hold-timeout-after-create" if create_clicked else "hold-application-not-ready",
    )
    if create_clicked:
        return (
            site_selectors.HoldResult(
                site_selectors.UNCERTAIN,
                f"Create Application was pressed but confirmation was not observed; screenshot: {captured.get('screenshot') or '<none>'}",
                url=getattr(page, "url", ""),
                timings=timings,
            ),
            observer.detail(),
        )
    return (
        site_selectors.HoldResult(
            site_selectors.FAILED,
            f"application never became actionable; screenshot: {captured.get('screenshot') or '<none>'}",
            url=getattr(page, "url", ""),
            timings=timings,
        ),
        observer.detail(),
    )

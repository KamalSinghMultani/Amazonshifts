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

import failure_capture
import hold_verify
import site_selectors


log = logging.getLogger("watcher")

NEXT = "[data-test-id='layout'] button:has-text('Next')"
CREATE = "[data-test-id='layout'] button:has-text('Create Application')"
INTEGRITY_AGREE = "[data-test-id='integrity-notice-agree-button']"
INTEGRITY_ROUTE = "application-integrity-notice"
HOLD_TEXT = "holding a spot"


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


def _banner(page: Any) -> str:
    try:
        text = page.inner_text("body") or ""
    except Exception:
        return ""
    low = text.lower()
    idx = low.find(HOLD_TEXT)
    if idx < 0:
        return ""
    return " ".join(text[idx:idx + 220].split())


def _integrity_notice(page: Any) -> bool:
    """Whether Amazon advanced to its applicant integrity attestation."""
    try:
        if INTEGRITY_ROUTE in (getattr(page, "url", "") or "").lower():
            return True
    except Exception:
        pass
    return _visible(page, INTEGRITY_AGREE)


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
) -> tuple[site_selectors.HoldResult, str]:
    """Return (HoldResult, backend_detail) with minimal local waiting.

    Polling is 50ms and readiness-driven. After Create Application is pressed,
    return immediately when Amazon's own update-application response proves
    JOB_SELECTED + expected schedule + reserve expiry.

    The explicit real-hold validation can keep this observer alive at Amazon's
    Application Integrity Notice while the applicant clicks I Agree manually in
    the visible browser. This function only observes that transition; it never
    clicks the integrity attestation itself.
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
            create_ready = _enabled(page, CREATE)
            if (next_ready or create_ready) and not overlay_checked:
                mark("application action ready")
                site_selectors.dismiss_overlays(
                    page, timeout_ms=min(timeout_ms, 1500), rounds=2
                )
                overlay_checked = True
                next_ready = _enabled(page, NEXT)
                create_ready = _enabled(page, CREATE)

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

                    if not manual_integrity_wait:
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

                    # Test-only manual handoff. Keep the same response observer
                    # attached while the applicant acts in the visible browser.
                    site_selectors.dismiss_overlays(
                        page, timeout_ms=min(timeout_ms, 1500), rounds=2
                    )
                    deadline = max(
                        deadline,
                        time.perf_counter()
                        + max(1000, int(manual_integrity_timeout_ms)) / 1000.0,
                    )
                    log.warning(
                        "MANUAL ACTION REQUIRED — Amazon's Application Integrity Notice is open. "
                        "Click I Agree yourself in the visible browser. The watcher will only "
                        "observe what Amazon does next and will not click that attestation."
                    )

                if integrity_seen and not integrity_left and not on_integrity:
                    integrity_left = True
                    mark("integrity notice left manually")
                    # Once the applicant has acted, give Amazon a fresh backend
                    # confirmation window rather than consuming the original
                    # application-load deadline.
                    deadline = max(
                        deadline,
                        time.perf_counter() + max(timeout_ms, 20000) / 1000.0,
                    )
                    log.info(
                        "manual integrity step left; waiting for reserve confirmation or next page"
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

    if integrity_seen and manual_integrity_wait:
        category = (
            "hold-after-manual-integrity-no-confirmation"
            if integrity_left
            else "hold-manual-integrity-timeout"
        )
        captured = failure_capture.capture(
            page,
            getattr(page, "context", None),
            base_url,
            category,
            extra={
                "reserve_confirmed": False,
                "integrity_left_manually": integrity_left,
            },
        )
        message = (
            "The Application Integrity Notice was left manually, but no reserve confirmation "
            "was observed. Check the current page and screenshot."
            if integrity_left
            else "Timed out waiting for the applicant to complete the Application Integrity Notice."
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

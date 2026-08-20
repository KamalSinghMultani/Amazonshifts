"""Latency-first browser-driven hold path.

This deliberately does NOT replay Amazon's candidate-application APIs.  It
navigates the same application URL and lets Amazon's own frontend issue the
create/update calls.  The only backend interaction here is passive response
observation via hold_verify.SoftReserveObserver.
"""

from __future__ import annotations

import time
from typing import Any

import failure_capture
import hold_verify
import site_selectors


NEXT = "[data-test-id='layout'] button:has-text('Next')"
CREATE = "[data-test-id='layout'] button:has-text('Create Application')"
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


def hold(
    page: Any,
    application_url: str,
    expected_schedule_id: str,
    *,
    base_url: str,
    stop_before_submit: bool,
    timeout_ms: int,
    screenshot_path: str | None = None,
) -> tuple[site_selectors.HoldResult, str]:
    """Return (HoldResult, backend_detail) with minimal local waiting.

    Polling is 50ms and readiness-driven.  After Create Application is pressed,
    the function returns immediately when Amazon's own update-application
    response proves JOB_SELECTED + expected schedule + reserve expiry.
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
                page, getattr(page, "context", None), base_url, "hold-navigation",
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
                    page, getattr(page, "context", None), base_url, "hold-login-redirect"
                )
                return (
                    site_selectors.HoldResult(
                        site_selectors.FAILED,
                        f"application redirected to login; screenshot: {captured.get('screenshot') or '<none>'}",
                        url=getattr(page, "url", ""), timings=timings,
                    ),
                    observer.detail(),
                )

            # Overlay work is deferred until an actionable application control
            # exists.  This avoids paying modal-probe cost while React is still
            # mounting.
            next_ready = _enabled(page, NEXT)
            create_ready = _enabled(page, CREATE)
            if (next_ready or create_ready) and not overlay_checked:
                mark("application action ready")
                site_selectors.dismiss_overlays(page, timeout_ms=min(timeout_ms, 1500), rounds=2)
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
                if screenshot_path:
                    try:
                        page.screenshot(path=screenshot_path, full_page=False)
                    except Exception:
                        pass
                mark("create application ready")
                if stop_before_submit:
                    return (
                        site_selectors.HoldResult(
                            site_selectors.FAILED,
                            "stopped before Create Application by configuration",
                            url=getattr(page, "url", ""), timings=timings,
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
                        page, getattr(page, "context", None), base_url, "hold-create-click",
                        extra={"error": str(exc)[:200]},
                    )
                    return (
                        site_selectors.HoldResult(
                            site_selectors.FAILED,
                            f"could not press Create Application: {str(exc)[:120]}; screenshot: {captured.get('screenshot') or '<none>'}",
                            url=getattr(page, "url", ""), timings=timings,
                        ),
                        observer.detail(),
                    )

            if create_clicked:
                banner = _banner(page)
                if banner:
                    mark("holding banner seen")
                    return (
                        site_selectors.HoldResult(
                            site_selectors.CONFIRMED,
                            f"SPOT HELD — {banner}",
                            url=getattr(page, "url", ""), banner=banner, timings=timings,
                        ),
                        observer.detail(),
                    )

            try:
                page.wait_for_timeout(50)
            except Exception:
                break

    captured = failure_capture.capture(
        page, getattr(page, "context", None), base_url,
        "hold-timeout-after-create" if create_clicked else "hold-application-not-ready",
    )
    if create_clicked:
        return (
            site_selectors.HoldResult(
                site_selectors.UNCERTAIN,
                f"Create Application was pressed but confirmation was not observed; screenshot: {captured.get('screenshot') or '<none>'}",
                url=getattr(page, "url", ""), timings=timings,
            ),
            observer.detail(),
        )
    return (
        site_selectors.HoldResult(
            site_selectors.FAILED,
            f"application never became actionable; screenshot: {captured.get('screenshot') or '<none>'}",
            url=getattr(page, "url", ""), timings=timings,
        ),
        observer.detail(),
    )

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
IDENTITY_START = (
    "button:has-text('Start identity verification'), "
    "button:has-text('Start identification'), "
    "[role='button']:has-text('Start identity verification'), "
    "[role='button']:has-text('Start identification')"
)
IDENTITY_CONSENT_CHECKBOXES = "input[type='checkbox']"
HOLD_TEXT = "holding a spot"
UNAVAILABLE_TEXT_PATTERNS = (
    "at present, all shifts have been filled for this job",
    "all shifts have been filled for this job",
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
UNAVAILABLE_ROUTE_PATTERNS = (
    "/no-available-shift",
    "/schedule-unavailable",
)
IDENTITY_TEXT_PATTERNS = (
    "let's confirm it's you",
    "let’s confirm it’s you",
    "start identity verification",
    "start identification",
)
ACTUAL_IDENTITY_TEXT_PATTERNS = (
    "take a selfie",
    "upload your identity document",
    "scan your identity document",
    "take a photo of your identity document",
)
IDENTITY_CONSENT_TEXT_PATTERNS = (
    "i agree that amazon and its service providers may use artificial intelligence and machine learning",
    "i consent to the collection and processing of my personal information",
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


def _pointer_actionable(page: Any, selector: str, *, timeout_ms: int = 200) -> bool:
    """Use Playwright's trial action to prove a control can receive a click.

    Visible + enabled is insufficient when a transparent modal backdrop is on
    top. ``trial=True`` runs the normal browser actionability checks without
    firing the click, so timing labels and submit decisions reflect reality.
    """
    try:
        item = page.locator(selector).first
        if item.count() == 0 or not item.is_visible() or not item.is_enabled():
            return False
        item.click(timeout=max(1, int(timeout_ms)), trial=True)
        return True
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
    try:
        url = (getattr(page, "url", "") or "").lower()
        matched_route = next(
            (pattern for pattern in UNAVAILABLE_ROUTE_PATTERNS if pattern in url),
            "",
        )
        if matched_route:
            return f"Amazon routed the application to {matched_route}."
    except Exception:
        pass
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


def _identity_consent_page(page: Any) -> bool:
    """Match only Amazon's two-checkbox identity-consent launcher page."""
    text = _body_text(page).lower().replace("(ai/ml)", "")
    return all(pattern in text for pattern in IDENTITY_CONSENT_TEXT_PATTERNS)


def _identity_consent_checkboxes(page: Any) -> Any:
    """Prefer accessible checkbox roles, with a CSS fallback for test doubles."""
    get_by_role = getattr(page, "get_by_role", None)
    if callable(get_by_role):
        return get_by_role("checkbox")
    return page.locator(IDENTITY_CONSENT_CHECKBOXES)


def _actual_identity_verification_active(
    page: Any, *, after_launcher_click: bool = False
) -> bool:
    """Detect the real KYC experience after the safe launcher was pressed.

    The hiring.amazon.ca launcher is allowed to perform Amazon's already-
    verified-account check.  remoteKYC, camera/selfie, and document screens
    remain strictly manual.
    """
    try:
        url = (getattr(page, "url", "") or "").lower()
        if "remotekyc" in url:
            return True
    except Exception:
        pass
    if not after_launcher_click:
        return False
    # The launcher page can describe the later steps. Only treat that text as
    # the real KYC UI once its Start control has disappeared.
    if _visible(page, IDENTITY_START):
        return False
    text = _body_text(page).lower()
    return any(pattern in text for pattern in ACTUAL_IDENTITY_TEXT_PATTERNS)


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
    auto_accept_identity_consent_and_start: bool = False,
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

    ``auto_accept_identity_consent_and_start`` is an explicit opt-in to check
    the two identity/privacy consent boxes shown by Amazon Hiring and click its
    Start identification launcher once. This supports accounts for which Amazon
    then recognizes a previously completed identity check. It never operates
    remoteKYC, selfie, camera, document-upload, or submission controls.

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
        application_action_ready_seen = False
        agree_visible_seen = False
        agree_enabled_seen = False
        agree_actionable_seen = False
        observer_update_marked = False
        integrity_overlay_checked = False
        integrity_click_error = ""
        create_click_error = ""
        identity_seen = False
        identity_left = False
        identity_start_clicked = False
        identity_start_visible_seen = False
        identity_start_enabled_seen = False
        identity_start_actionable_seen = False
        identity_start_click_error = ""
        identity_consent_visible_seen = [False, False]
        identity_consent_enabled_seen = [False, False]
        identity_consent_actionable_seen = [False, False]
        identity_consent_checked_seen = [False, False]
        identity_consent_click_errors = ["", ""]
        identity_handoff_captured = False

        def capture_identity_handoff() -> None:
            nonlocal identity_handoff_captured
            if identity_handoff_captured or not screenshot_path:
                return
            try:
                page.screenshot(path=screenshot_path, full_page=False)
                identity_handoff_captured = True
                mark("identity handoff screenshot captured")
            except Exception:  # noqa: BLE001
                pass

        def identity_result(message: str) -> tuple[site_selectors.HoldResult, str]:
            # Capture the page Amazon produced after the already-authorized
            # launcher click.  This is a diagnostic/manual handoff only; no
            # remoteKYC control is inspected or operated.
            capture_identity_handoff()
            return (
                site_selectors.HoldResult(
                    site_selectors.IDENTITY_VERIFICATION_REQUIRED,
                    message,
                    url=_safe_result_url(page, application_url),
                    timings=timings,
                ),
                observer.detail(),
            )

        while time.perf_counter() < deadline:
            if observer.confirmed:
                if identity_start_clicked:
                    capture_identity_handoff()
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

            actual_identity = _actual_identity_verification_active(
                page,
                after_launcher_click=identity_start_clicked,
            )
            if actual_identity:
                mark("actual identity verification required")
                return identity_result(
                    "Amazon opened the actual identity-verification experience. "
                    "Application automation stopped before selfie, ID upload, or submission; "
                    "complete it manually on Amazon if you choose."
                )

            identity_required = _identity_verification_required(page)
            if identity_start_clicked and not identity_required and not identity_left:
                identity_left = True
                mark("identity verification launcher skipped")
                log.info(
                    "identity launcher left after one normal Start identification click; continuing reserve observation"
                )

            if identity_required:
                if not identity_seen:
                    identity_seen = True
                    mark("identity verification launcher reached")

                if not auto_accept_identity_consent_and_start:
                    mark("identity verification required")
                    return identity_result(
                        "Amazon requires identity verification for this candidate account. "
                        "Application automation stopped before consent, selfie, ID upload, or submission; "
                        "complete the verification manually on Amazon if you choose."
                    )

                consent_ready = False
                if _identity_consent_page(page):
                    try:
                        checkboxes = _identity_consent_checkboxes(page)
                        if checkboxes.count() >= 2:
                            checked_states: list[bool] = []
                            for index in range(2):
                                checkbox = checkboxes.nth(index)
                                label = f"identity consent {index + 1}"
                                visible = checkbox.is_visible()
                                if visible and not identity_consent_visible_seen[index]:
                                    identity_consent_visible_seen[index] = True
                                    mark(f"{label} visible")
                                enabled = visible and checkbox.is_enabled()
                                if enabled and not identity_consent_enabled_seen[index]:
                                    identity_consent_enabled_seen[index] = True
                                    mark(f"{label} enabled")
                                checked = bool(checkbox.is_checked())
                                if checked and not identity_consent_checked_seen[index]:
                                    identity_consent_checked_seen[index] = True
                                    mark(f"{label} checked")
                                if not checked and enabled:
                                    try:
                                        checkbox.click(
                                            timeout=min(timeout_ms, 200),
                                            trial=True,
                                        )
                                        if not identity_consent_actionable_seen[index]:
                                            identity_consent_actionable_seen[index] = True
                                            mark(f"{label} actionable")
                                        checkbox.click(timeout=min(timeout_ms, 1000))
                                        mark(f"{label} clicked")
                                        page.wait_for_timeout(10)
                                        checked = bool(checkbox.is_checked())
                                        if checked and not identity_consent_checked_seen[index]:
                                            identity_consent_checked_seen[index] = True
                                            mark(f"{label} checked")
                                    except Exception as exc:  # noqa: BLE001
                                        identity_consent_click_errors[index] = type(exc).__name__
                                checked_states.append(checked)
                            consent_ready = all(checked_states)
                    except Exception as exc:  # noqa: BLE001
                        identity_consent_click_errors[0] = type(exc).__name__

                # Do not click a generic checkbox on an unexpected page, and
                # do not click Start until both exact consent controls report
                # checked. The next loop re-probes React state without a fixed
                # sleep or force/JavaScript click.
                if not consent_ready:
                    try:
                        page.wait_for_timeout(50)
                    except Exception:
                        break
                    continue

                start_visible = _visible(page, IDENTITY_START)
                if start_visible and not identity_start_visible_seen:
                    identity_start_visible_seen = True
                    mark("start identification visible")
                start_enabled = _enabled(page, IDENTITY_START)
                if start_enabled and not identity_start_enabled_seen:
                    identity_start_enabled_seen = True
                    mark("start identification enabled")

                if start_enabled and not identity_start_clicked:
                    start_actionable = _pointer_actionable(
                        page,
                        IDENTITY_START,
                        timeout_ms=min(timeout_ms, 200),
                    )
                    if start_actionable:
                        if not identity_start_actionable_seen:
                            identity_start_actionable_seen = True
                            mark("start identification actionable")
                        try:
                            page.locator(IDENTITY_START).first.click(
                                timeout=min(timeout_ms, 1000)
                            )
                            identity_start_clicked = True
                            mark("start identification clicked")
                            deadline = max(
                                deadline,
                                time.perf_counter() + max(timeout_ms, 20000) / 1000.0,
                            )
                            log.info(
                                "Start identification clicked once; waiting only for Amazon's already-verified skip or a terminal state"
                            )
                            page.wait_for_timeout(10)
                            continue
                        except Exception as exc:  # noqa: BLE001
                            identity_start_click_error = type(exc).__name__

                # Wait for the launcher button/automatic redirect within the
                # existing bounded post-I-Agree window. Do not touch camera,
                # document, or remoteKYC controls.
                try:
                    page.wait_for_timeout(50)
                except Exception:
                    break
                continue

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
            if (next_ready or create_ready) and (
                not overlay_checked or site_selectors.blocking_overlay_visible(page)
            ):
                if not application_action_ready_seen:
                    application_action_ready_seen = True
                    mark("application action ready")
                dismissed = site_selectors.dismiss_overlays(
                    page, timeout_ms=min(timeout_ms, 1500), rounds=2
                )
                if dismissed:
                    mark("application overlay dismissed")
                overlay_checked = not site_selectors.blocking_overlay_visible(page)
                next_ready = _enabled(page, NEXT)
                create_ready = _enabled(page, CREATE)

            next_actionable = (
                next_ready
                and overlay_checked
                and _pointer_actionable(page, NEXT, timeout_ms=min(timeout_ms, 200))
            )
            create_actionable = (
                create_ready
                and overlay_checked
                and _pointer_actionable(page, CREATE, timeout_ms=min(timeout_ms, 200))
            )

            if create_actionable and not create_actionable_seen:
                create_actionable_seen = True
                mark("create application actionable")

            if not next_clicked and next_actionable:
                try:
                    page.locator(NEXT).first.click(timeout=min(timeout_ms, 1000))
                    next_clicked = True
                    overlay_checked = False
                    mark("next clicked")
                    page.wait_for_timeout(25)
                    continue
                except Exception:
                    pass

            if create_actionable and not create_clicked:
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
                    page.locator(CREATE).first.click(timeout=min(timeout_ms, 1000))
                    create_clicked = True
                    mark("create application clicked")
                    # Give Playwright an event-loop turn so response handlers can
                    # consume an already-fast backend response immediately.
                    page.wait_for_timeout(10)
                    continue
                except Exception as exc:  # noqa: BLE001
                    # Actionability can change between the trial and real click
                    # if Amazon mounts another overlay. Re-probe and retry
                    # within the existing bounded deadline instead of failing
                    # the whole attempt after one transient race.
                    create_click_error = type(exc).__name__
                    overlay_checked = False
                    mark("create application click retry")
                    page.wait_for_timeout(10)
                    continue

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
                        if (
                            not integrity_overlay_checked
                            or site_selectors.blocking_overlay_visible(page)
                        ):
                            dismissed = site_selectors.dismiss_overlays(
                                page, timeout_ms=min(timeout_ms, 1500), rounds=2
                            )
                            if dismissed:
                                mark("integrity overlay dismissed")
                            integrity_overlay_checked = not site_selectors.blocking_overlay_visible(page)
                            agree_enabled = _enabled(page, INTEGRITY_AGREE)
                        agree_actionable = (
                            agree_enabled
                            and integrity_overlay_checked
                            and _pointer_actionable(
                                page,
                                INTEGRITY_AGREE,
                                timeout_ms=min(timeout_ms, 200),
                            )
                        )
                        if agree_actionable:
                            if not agree_actionable_seen:
                                agree_actionable_seen = True
                                mark("integrity agree actionable")
                            try:
                                page.locator(INTEGRITY_AGREE).first.click(
                                    timeout=min(timeout_ms, 1000)
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
                                integrity_overlay_checked = False

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
                        if identity_start_clicked:
                            capture_identity_handoff()
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
                    if identity_start_clicked:
                        capture_identity_handoff()
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

    if identity_seen and not identity_left:
        mark("identity verification launcher timeout")
        if not identity_start_clicked:
            consent_error = next(
                (error for error in identity_consent_click_errors if error),
                "",
            )
            last_error = identity_start_click_error or consent_error
            detail = (
                "Amazon's identity consent and Start identification controls did not become actionable"
                + (
                    f" (last click error: {last_error})"
                    if last_error
                    else ""
                )
                + ". Application automation stopped without operating selfie, ID, or remoteKYC controls."
            )
        else:
            detail = (
                "Amazon did not skip the identity launcher after Start identification was clicked once. "
                "Application automation stopped without operating selfie, ID, or remoteKYC controls."
            )
        return identity_result(detail)

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
        extra={"create_click_error_type": create_click_error},
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
            "application never became pointer-actionable before the timeout"
            + (f" (last click error: {create_click_error})" if create_click_error else "")
            + f"; screenshot: {captured.get('screenshot') or '<none>'}",
            url=getattr(page, "url", ""),
            timings=timings,
        ),
        observer.detail(),
    )

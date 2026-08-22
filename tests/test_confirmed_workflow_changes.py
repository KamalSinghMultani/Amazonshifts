from __future__ import annotations

import inspect

import fast_hold
import site_selectors
import hold_verify
from notifier import TelegramNotifier
import schedules
import site_selectors
import watcher
import watcher_v3
from shift_matcher import Shift


class PassiveObserver:
    def __init__(self, _page, _schedule):
        self.confirmed = False
        self.relevant_update_seen = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def detail(self):
        return ""

    def settled_without_confirmation(self):
        return False


class HoldFlowPage:
    context = None

    def __init__(self, *, after_agree="liveness", after_identity="unavailable"):
        self.phase = "blank"
        self.after_agree = after_agree
        self.after_identity = after_identity
        self.url = "about:blank"
        self.clicked = []
        self.identity_consents = [False, False]

    def goto(self, url, **_kwargs):
        self.phase = "create"
        self.url = url

    def inner_text(self, _selector):
        if self.phase == "create":
            return "Create Application"
        if self.phase == "integrity":
            return "Application Integrity Notice I Agree"
        if self.phase == "liveness":
            return (
                "Let's confirm it's you Provide consent "
                "I agree that Amazon and its service providers may use Artificial Intelligence "
                "and Machine Learning (AI/ML). I consent to the collection and processing of "
                "my personal information. Start identity verification"
            )
        if self.phase == "unavailable":
            return "Sorry, this shift is no longer available."
        if self.phase == "remote_kyc":
            return "Take a selfie Upload your identity document"
        return "Personal information"

    def wait_for_timeout(self, _ms):
        return None

    def locator(self, selector):
        page = self

        class L:
            def __init__(self, index=None):
                self.index = index

            @property
            def first(self):
                return L(0)

            def nth(self, index):
                return L(index)

            def _present(self):
                if "Create Application" in selector:
                    return page.phase == "create"
                if "integrity-notice-agree-button" in selector:
                    return page.phase == "integrity"
                if "Start identity" in selector or "Start identification" in selector:
                    return page.phase == "liveness"
                if selector == fast_hold.IDENTITY_CONSENT_CHECKBOXES:
                    return page.phase == "liveness" and self.index in (0, 1)
                return False

            def count(self):
                if selector == fast_hold.IDENTITY_CONSENT_CHECKBOXES:
                    if page.phase != "liveness":
                        return 0
                    return 2 if self.index is None else 1
                return int(self._present())

            def is_visible(self):
                return self._present()

            def is_enabled(self):
                if "Start identity" in selector or "Start identification" in selector:
                    return self._present() and all(page.identity_consents)
                return self._present()

            def is_checked(self):
                if selector != fast_hold.IDENTITY_CONSENT_CHECKBOXES or self.index not in (0, 1):
                    return False
                return page.identity_consents[self.index]

            def click(self, **kwargs):
                if kwargs.get("trial"):
                    return None
                if selector == fast_hold.IDENTITY_CONSENT_CHECKBOXES:
                    page.clicked.append(f"Identity consent {self.index + 1}")
                    page.identity_consents[self.index] = True
                elif "Create Application" in selector:
                    page.clicked.append("Create Application")
                    page.phase = "integrity"
                    page.url = "https://hiring.amazon.ca/application/ca/#/application-integrity-notice"
                elif "integrity-notice-agree-button" in selector:
                    page.clicked.append("I Agree")
                    page.phase = page.after_agree
                    if page.phase == "liveness":
                        page.url = (
                            "https://hiring.amazon.ca/application/ca/#/liveness-check"
                            "?trackingId=secret-kyc-id"
                        )
                    else:
                        page.url = "https://hiring.amazon.ca/application/ca/#/personal-information"
                elif "Start identity" in selector or "Start identification" in selector:
                    page.clicked.append("Start identity verification")
                    page.phase = page.after_identity
                    if page.phase == "unavailable":
                        page.url = "https://hiring.amazon.ca/application/ca/#/schedule-unavailable"
                    elif page.phase == "remote_kyc":
                        page.url = "https://www.amazon.in/remoteKYC?trackingId=secret-kyc-id"
                    else:
                        page.url = "https://hiring.amazon.ca/application/ca/#/personal-information"

        return L()


def test_pointer_actionable_requires_receiving_pointer_events():
    class Page:
        receiving_events = False

        def locator(self, _selector):
            page = self

            class L:
                @property
                def first(self):
                    return self

                def count(self):
                    return 1

                def is_visible(self):
                    return True

                def is_enabled(self):
                    return True

                def click(self, **kwargs):
                    assert kwargs.get("trial") is True
                    if not page.receiving_events:
                        raise RuntimeError("backdrop intercepts pointer events")

            return L()

    page = Page()
    assert fast_hold._pointer_actionable(page, "button") is False
    page.receiving_events = True
    assert fast_hold._pointer_actionable(page, "button") is True


def test_delayed_cookie_consent_button_is_retried_until_modal_closes():
    class Page:
        def __init__(self):
            self.open = True
            self.consent_visibility_checks = 0
            self.clicked = []

        def locator(self, selector):
            page = self

            class L:
                @property
                def first(self):
                    return self

                def count(self):
                    if selector in {site_selectors.MODAL_BACKDROP, site_selectors.CONSENT_MODAL}:
                        return int(page.open)
                    if selector == site_selectors.CONSENT_BUTTON:
                        return int(page.open)
                    return 0

                def is_visible(self):
                    if not page.open:
                        return False
                    if selector == site_selectors.CONSENT_BUTTON:
                        page.consent_visibility_checks += 1
                        return page.consent_visibility_checks >= 2
                    return selector in {site_selectors.MODAL_BACKDROP, site_selectors.CONSENT_MODAL}

                def is_enabled(self):
                    return True

                def click(self, **kwargs):
                    assert "force" not in kwargs
                    page.clicked.append(selector)
                    page.open = False

            return L()

        def wait_for_timeout(self, _ms):
            return None

    page = Page()
    dismissed = site_selectors.dismiss_overlays(page, timeout_ms=100, rounds=4)
    assert dismissed == ["cookie consent"]
    assert page.clicked == [site_selectors.CONSENT_BUTTON]
    assert site_selectors.blocking_overlay_visible(page) is False


def test_cookie_consent_uses_keyboard_when_its_backdrop_intercepts_pointer():
    class Page:
        def __init__(self):
            self.open = True
            self.keys = []

        def locator(self, selector):
            page = self

            class L:
                @property
                def first(self):
                    return self

                def count(self):
                    return int(page.open) if selector in {
                        site_selectors.MODAL_BACKDROP,
                        site_selectors.CONSENT_MODAL,
                        site_selectors.CONSENT_BUTTON,
                    } else 0

                def is_visible(self):
                    return bool(page.open)

                def is_enabled(self):
                    return True

                def click(self, **kwargs):
                    assert "force" not in kwargs
                    raise RuntimeError("backdrop intercepts pointer events")

                def press(self, key, **_kwargs):
                    page.keys.append(key)
                    page.open = False

            return L()

        def wait_for_timeout(self, _ms):
            return None

    page = Page()
    dismissed = site_selectors.dismiss_overlays(page, timeout_ms=100, rounds=2)
    assert dismissed == ["cookie consent (keyboard)"]
    assert page.keys == ["Enter"]
    assert site_selectors.blocking_overlay_visible(page) is False


def test_liveness_route_and_text_are_identity_verification_states():
    route = HoldFlowPage()
    route.url = "https://hiring.amazon.ca/application/ca/#/liveness-check?trackingId=secret"
    route.phase = "next"
    assert fast_hold._identity_verification_required(route) is True

    text = HoldFlowPage()
    text.url = "https://hiring.amazon.ca/application/ca/#/consent"
    text.phase = "liveness"
    assert fast_hold._identity_verification_required(text) is True


def test_identity_consent_page_requires_both_exact_consent_statements():
    complete = HoldFlowPage()
    complete.phase = "liveness"
    assert fast_hold._identity_consent_page(complete) is True

    class UnrelatedCheckboxPage:
        def inner_text(self, _selector):
            return "Subscribe to updates Start identity verification"

    assert fast_hold._identity_consent_page(UnrelatedCheckboxPage()) is False


class _VisibleTextLocator:
    def __init__(self, *, visible=False, text=""):
        self.first = self
        self.visible = visible
        self.text = text

    def count(self):
        return 1 if self.visible else 0

    def is_visible(self):
        return self.visible

    def inner_text(self):
        return self.text


class _UnavailableDetailPage:
    url = "https://hiring.amazon.ca/app#/jobDetail?jobId=JOB-CA-test"

    def __init__(self, *, warning=False, body="", flyout=""):
        self.warning = warning
        self.body = body
        self.flyout = flyout

    def locator(self, selector):
        if selector == site_selectors.UNPOSTED_JOB_WARNING:
            return _VisibleTextLocator(visible=self.warning)
        if selector == site_selectors.SCHEDULE_FLYOUT:
            return _VisibleTextLocator(visible=bool(self.flyout), text=self.flyout)
        return _VisibleTextLocator()

    def inner_text(self, selector):
        assert selector == "body"
        return self.body


def test_unposted_job_warning_is_terminal_even_with_schedule_button_rendered():
    page = _UnavailableDetailPage(warning=True)
    assert site_selectors.job_detail_unavailable(page) is True


def test_unavailable_job_text_is_terminal_when_test_id_is_absent():
    page = _UnavailableDetailPage(
        body="Warning — This job is not available for application now. Click here to go back."
    )
    assert site_selectors.job_detail_unavailable(page) is True


def test_zero_schedule_panel_is_classified_without_waiting_for_apply():
    page = _UnavailableDetailPage(
        flyout="Select your schedule\nSchedule Hours\n0 schedules found\n"
        "Sorry, there are no schedules that match your filter choices."
    )
    assert site_selectors.schedule_flyout_empty(page) is True


def test_nonempty_schedule_panel_is_not_a_ghost_classification():
    page = _UnavailableDetailPage(
        flyout="Select your schedule\nSun, Mon, Tue, Wed 3:00 PM - 8:30 PM\nApply"
    )
    assert site_selectors.schedule_flyout_empty(page) is False


def test_create_and_agree_click_as_soon_as_actionable_then_stop_at_ekyc(monkeypatch):
    monkeypatch.setattr(fast_hold.hold_verify, "SoftReserveObserver", PassiveObserver)
    monkeypatch.setattr(fast_hold.site_selectors, "dismiss_overlays", lambda *_a, **_k: [])
    page = HoldFlowPage(after_agree="liveness")

    result, _detail = fast_hold.hold(
        page,
        "https://hiring.amazon.ca/application/ca/?jobId=JOB-1&scheduleId=SCH-1",
        "SCH-1",
        base_url="https://hiring.amazon.ca",
        stop_before_submit=False,
        timeout_ms=1000,
        auto_integrity_agree=True,
    )

    assert result.status == site_selectors.IDENTITY_VERIFICATION_REQUIRED
    assert page.clicked == ["Create Application", "I Agree"]
    assert "secret-kyc-id" not in result.url
    assert "secret-kyc-id" not in result.message
    names = [name for name, _ms in result.timings]
    assert names.index("create application visible") < names.index("create application clicked")
    assert names.index("create application enabled") < names.index("create application clicked")
    assert names.index("create application actionable") < names.index("create application clicked")
    assert names.index("integrity agree visible") < names.index("integrity agree clicked")
    assert names.index("integrity agree enabled") < names.index("integrity agree clicked")
    assert names.index("integrity agree actionable") < names.index("integrity agree clicked")
    assert names.index("integrity agree clicked") < names.index("identity verification required")


def test_completed_identity_launcher_is_clicked_once_then_hold_flow_continues(monkeypatch):
    monkeypatch.setattr(fast_hold.hold_verify, "SoftReserveObserver", PassiveObserver)
    monkeypatch.setattr(fast_hold.site_selectors, "dismiss_overlays", lambda *_a, **_k: [])
    monkeypatch.setattr(
        fast_hold.failure_capture,
        "capture",
        lambda *_a, **_k: {"screenshot": "safe.png"},
    )
    page = HoldFlowPage(after_agree="liveness", after_identity="unavailable")

    result, _detail = fast_hold.hold(
        page,
        "https://hiring.amazon.ca/application/ca/?jobId=JOB-1&scheduleId=SCH-1",
        "SCH-1",
        base_url="https://hiring.amazon.ca",
        stop_before_submit=False,
        timeout_ms=1000,
        auto_integrity_agree=True,
        auto_accept_identity_consent_and_start=True,
    )

    assert result.status == site_selectors.FAILED
    assert "no longer available" in result.message.lower()
    assert page.clicked == [
        "Create Application",
        "I Agree",
        "Identity consent 1",
        "Identity consent 2",
        "Start identity verification",
    ]
    names = [name for name, _ms in result.timings]
    assert names.index("identity consent 1 clicked") < names.index("identity consent 2 clicked")
    assert names.index("identity consent 2 checked") < names.index("start identification clicked")
    assert names.index("start identification visible") < names.index("start identification clicked")
    assert names.index("start identification enabled") < names.index("start identification clicked")
    assert names.index("start identification actionable") < names.index("start identification clicked")
    assert names.index("start identification clicked") < names.index("identity verification launcher skipped")
    assert names.index("identity verification launcher skipped") < names.index("schedule unavailable after integrity")


def test_actual_remote_kyc_stops_immediately_after_launcher_click(monkeypatch):
    monkeypatch.setattr(fast_hold.hold_verify, "SoftReserveObserver", PassiveObserver)
    monkeypatch.setattr(fast_hold.site_selectors, "dismiss_overlays", lambda *_a, **_k: [])
    page = HoldFlowPage(after_agree="liveness", after_identity="remote_kyc")

    result, _detail = fast_hold.hold(
        page,
        "https://hiring.amazon.ca/application/ca/?jobId=JOB-1&scheduleId=SCH-1",
        "SCH-1",
        base_url="https://hiring.amazon.ca",
        stop_before_submit=False,
        timeout_ms=1000,
        auto_integrity_agree=True,
        auto_accept_identity_consent_and_start=True,
    )

    assert result.status == site_selectors.IDENTITY_VERIFICATION_REQUIRED
    assert page.clicked == [
        "Create Application",
        "I Agree",
        "Identity consent 1",
        "Identity consent 2",
        "Start identity verification",
    ]
    assert "tracking" not in result.url.lower()
    assert "tracking" not in result.message.lower()
    names = [name for name, _ms in result.timings]
    assert names.index("start identification clicked") < names.index("actual identity verification required")


def test_actual_remote_kyc_handoff_is_screenshotted_after_start(monkeypatch, tmp_path):
    monkeypatch.setattr(fast_hold.hold_verify, "SoftReserveObserver", PassiveObserver)
    monkeypatch.setattr(fast_hold.site_selectors, "dismiss_overlays", lambda *_a, **_k: [])

    class ScreenshotPage(HoldFlowPage):
        def screenshot(self, *, path, **_kwargs):
            from pathlib import Path
            Path(path).write_bytes(b"safe-test-image")

    page = ScreenshotPage(after_agree="liveness", after_identity="remote_kyc")
    shot = tmp_path / "identity-handoff.png"
    result, _detail = fast_hold.hold(
        page,
        "https://hiring.amazon.ca/application/ca/?jobId=JOB-1&scheduleId=SCH-1",
        "SCH-1",
        base_url="https://hiring.amazon.ca",
        stop_before_submit=False,
        timeout_ms=1000,
        screenshot_path=str(shot),
        auto_integrity_agree=True,
        auto_accept_identity_consent_and_start=True,
    )

    assert result.status == site_selectors.IDENTITY_VERIFICATION_REQUIRED
    assert shot.exists()
    names = [name for name, _ms in result.timings]
    assert names.index("start identification clicked") < names.index(
        "identity handoff screenshot captured"
    )


def test_completed_identity_skip_can_finish_from_reserve_proof(monkeypatch):
    class ConfirmAfterIdentity(PassiveObserver):
        def __init__(self, page, schedule):
            self.page = page
            self.schedule = schedule
            self.relevant_update_seen = False

        @property
        def confirmed(self):
            return self.page.phase == "confirmed"

        def detail(self):
            return "backend soft reserve confirmed"

    monkeypatch.setattr(fast_hold.hold_verify, "SoftReserveObserver", ConfirmAfterIdentity)
    monkeypatch.setattr(fast_hold.site_selectors, "dismiss_overlays", lambda *_a, **_k: [])
    page = HoldFlowPage(after_agree="liveness", after_identity="confirmed")

    result, detail = fast_hold.hold(
        page,
        "https://hiring.amazon.ca/application/ca/?jobId=JOB-1&scheduleId=SCH-1",
        "SCH-1",
        base_url="https://hiring.amazon.ca",
        stop_before_submit=False,
        timeout_ms=1000,
        auto_integrity_agree=True,
        auto_accept_identity_consent_and_start=True,
    )

    assert result.status == site_selectors.CONFIRMED
    assert detail == "backend soft reserve confirmed"
    assert page.clicked == [
        "Create Application",
        "I Agree",
        "Identity consent 1",
        "Identity consent 2",
        "Start identity verification",
    ]
    names = [name for name, _ms in result.timings]
    assert names.index("start identification clicked") < names.index("backend reserve confirmed")


def test_relevant_post_agree_update_can_end_as_uncertain_without_full_timeout(monkeypatch):
    class SettledObserver(PassiveObserver):
        def __init__(self, page, schedule):
            super().__init__(page, schedule)
            self.relevant_update_seen = True

        def settled_without_confirmation(self):
            return True

    monkeypatch.setattr(fast_hold.hold_verify, "SoftReserveObserver", SettledObserver)
    monkeypatch.setattr(fast_hold.site_selectors, "dismiss_overlays", lambda *_a, **_k: [])
    monkeypatch.setattr(
        fast_hold.failure_capture,
        "capture",
        lambda *_a, **_k: {"screenshot": "safe.png"},
    )
    page = HoldFlowPage(after_agree="next")

    result, _detail = fast_hold.hold(
        page,
        "https://hiring.amazon.ca/application/ca/?jobId=JOB-1&scheduleId=SCH-1",
        "SCH-1",
        base_url="https://hiring.amazon.ca",
        stop_before_submit=False,
        timeout_ms=1000,
        auto_integrity_agree=True,
    )

    assert result.status == site_selectors.UNCERTAIN
    assert "did not contain complete reserve proof" in result.message
    names = [name for name, _ms in result.timings]
    assert "post integrity update response observed" in names
    assert "post integrity update lacked reserve proof" in names


def test_passive_observer_only_accelerates_uncertain_after_relevant_settled_update(monkeypatch):
    handlers = {}

    class Page:
        def on(self, name, handler):
            handlers[name] = handler

        def remove_listener(self, *_args):
            pass

    class Response:
        url = "https://hiring.amazon.ca/application/api/candidate-application/update-application"

        def json(self):
            return {
                "data": {
                    "currentState": "APPLICATION_STARTED",
                    "jobScheduleSelected": {"scheduleId": "SCH-1"},
                    "softReserveExpirationTimestamp": None,
                }
            }

    clock = iter([10.0, 12.0])
    monkeypatch.setattr(hold_verify.time, "monotonic", lambda: next(clock))
    observer = hold_verify.SoftReserveObserver(Page(), "SCH-1")
    with observer:
        handlers["response"](Response())
        assert observer.confirmed is False
        assert observer.relevant_update_seen is True
        assert observer.settled_without_confirmation(quiet_ms=1500) is True


def test_identity_state_notifies_and_stops_retrying_the_same_attempt():
    result = site_selectors.HoldResult(
        site_selectors.IDENTITY_VERIFICATION_REQUIRED,
        "identity verification required",
    )
    assert result.needs_you is True
    assert result.worth_retrying() is False

    loop = inspect.getsource(watcher_v3.OptimizedWatcher.poll_once)
    report = inspect.getsource(watcher.Watcher._report_hold)
    assert "site_selectors.IDENTITY_VERIFICATION_REQUIRED" in loop
    assert "notify_identity_verification" in report


def test_manual_identity_url_has_only_public_job_and_schedule_ids():
    url = schedules.identity_verification_url(
        "https://hiring.amazon.ca",
        "JOB-CA-1",
        "SCH-CA-2",
    )
    assert url == (
        "https://hiring.amazon.ca/application/ca/"
        "?jobId=JOB-CA-1&scheduleId=SCH-CA-2"
        "#/liveness-check?jobId=JOB-CA-1&scheduleId=SCH-CA-2"
    )
    assert "tracking" not in url.lower()


def test_identity_alert_has_clickable_safe_manual_link():
    notifier = TelegramNotifier(enabled=False)
    sent = []
    notifier.send_text = lambda text: sent.append(text) or True
    manual = schedules.identity_verification_url(
        "https://hiring.amazon.ca", "JOB-1", "SCH-2"
    )
    notifier.notify_identity_verification(
        Shift(title="Warehouse", location="Barrhaven, ON"),
        manual,
        detail="Complete manually.",
    )

    assert '<a href="https://hiring.amazon.ca/application/ca/' in sent[0]
    assert "Open Amazon identity verification</a>" in sent[0]
    assert "JOB-1" in sent[0] and "SCH-2" in sent[0]
    assert "trackingId" not in sent[0]


def test_identity_result_dispatches_dedicated_clickable_alert_immediately(tmp_path):
    class Notifier:
        def notify_identity_verification(self, *_args, **_kwargs):
            return True

        def send_photo(self, *_args, **_kwargs):
            return True

    dispatched = []
    instance = watcher.Watcher.__new__(watcher.Watcher)
    instance.cfg = {"site": {"base_url": "https://hiring.amazon.ca"}}
    instance.notifier = Notifier()
    instance.last_hold = None
    instance.notify_async = lambda fn, *args, **kwargs: dispatched.append(
        (fn.__name__, args, kwargs)
    )
    shift = Shift(
        title="Warehouse",
        raw={"jobId": "JOB-CA-1", "scheduleId": "SCH-CA-2"},
    )
    result = site_selectors.HoldResult(
        site_selectors.IDENTITY_VERIFICATION_REQUIRED,
        "Complete identity verification manually.",
        url="https://hiring.amazon.ca/#/liveness-check?trackingId=private",
    )

    instance._report_hold(shift, result, tmp_path / "missing.png")

    assert dispatched[0][0] == "notify_identity_verification"
    assert dispatched[0][1][1] == schedules.identity_verification_url(
        "https://hiring.amazon.ca", "JOB-CA-1", "SCH-CA-2"
    )


def test_failed_hold_telegram_url_is_rebuilt_from_public_ids_not_result_url(tmp_path):
    class Recording:
        def notify_hold_attention(self, *_args, **_kwargs):
            return True

        def send_photo(self, *_args, **_kwargs):
            return True

    instance = object.__new__(watcher.Watcher)
    instance.cfg = {"site": {"base_url": "https://hiring.amazon.ca"}}
    instance.notifier = Recording()
    instance.last_hold = None
    dispatched = []
    instance.notify_async = lambda fn, *args, **kwargs: dispatched.append(
        (fn.__name__, args, kwargs)
    )
    shift = Shift(
        id="SCH-1",
        title="Warehouse",
        raw={"jobId": "JOB-1", "scheduleId": "SCH-1"},
    )
    result = site_selectors.HoldResult(
        site_selectors.FAILED,
        "not available",
        url="https://www.amazon.in/remoteKYC?trackingId=secret",
    )
    instance._report_hold(shift, result, tmp_path / "missing.png")
    assert dispatched[0][0] == "notify_hold_attention"
    manual_url = dispatched[0][1][3]
    assert manual_url == schedules.application_url(
        "https://hiring.amazon.ca", "JOB-1", "SCH-1"
    )
    assert "tracking" not in manual_url.lower()
    assert "tracking" not in dispatched[0][1][1].lower()

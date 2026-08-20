from __future__ import annotations

import inspect

import fast_hold
import hold_verify
import site_selectors
import watcher
import watcher_v3


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

    def __init__(self, *, after_agree="liveness"):
        self.phase = "blank"
        self.after_agree = after_agree
        self.url = "about:blank"
        self.clicked = []

    def goto(self, url, **_kwargs):
        self.phase = "create"
        self.url = url

    def inner_text(self, _selector):
        if self.phase == "create":
            return "Create Application"
        if self.phase == "integrity":
            return "Application Integrity Notice I Agree"
        if self.phase == "liveness":
            return "Let's confirm it's you Start identity verification"
        return "Personal information"

    def wait_for_timeout(self, _ms):
        return None

    def locator(self, selector):
        page = self

        class L:
            @property
            def first(self):
                return self

            def _present(self):
                if "Create Application" in selector:
                    return page.phase == "create"
                if "integrity-notice-agree-button" in selector:
                    return page.phase == "integrity"
                return False

            def count(self):
                return int(self._present())

            def is_visible(self):
                return self._present()

            def is_enabled(self):
                return self._present()

            def click(self, **kwargs):
                if kwargs.get("trial"):
                    return None
                if "Create Application" in selector:
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
    assert "IDENTITY VERIFICATION REQUIRED" in report

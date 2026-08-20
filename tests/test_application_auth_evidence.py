from __future__ import annotations

import relogin
import relogin_patch


relogin_patch.apply_patch(relogin)


class _Locator:
    def __init__(self, visible=False):
        self._visible = visible
        self.first = self

    def count(self):
        return 1 if self._visible else 0

    def is_visible(self):
        return self._visible


class FakePage:
    def __init__(self, *, url, body_text="", evidence=None):
        self.url = url
        self.body_text = body_text
        self.evidence = evidence or {}

    def locator(self, _selector):
        return _Locator(False)

    def inner_text(self, _selector):
        return self.body_text

    def evaluate(self, _script):
        return dict(self.evidence)


def _live_consent_evidence(**overrides):
    evidence = {
        "routeConsent": True,
        "layoutVisible": True,
        "titleConsent": True,
        "bodyConsent": False,
        "createVisible": False,
        "loginVisible": False,
        "tokenStructure": True,
    }
    evidence.update(overrides)
    return evidence


def test_live_protected_application_consent_counts_as_authenticated():
    page = FakePage(
        url="https://hiring.amazon.ca/application/ca/#/consent",
        evidence=_live_consent_evidence(),
    )
    detector = relogin.StateDetector(page)

    assert detector._is_authenticated() is True
    assert detector.detect_state() == relogin.AuthState.AUTHENTICATED


def test_locale_header_text_does_not_override_protected_application_proof():
    # The normal Hiring header can contain the same wording the auth page uses.
    # Protected consent evidence must be evaluated before generic body phrases.
    page = FakePage(
        url="https://hiring.amazon.ca/application/ca/#/consent",
        body_text="Select your country and language\nBy applying, you confirm that:",
        evidence=_live_consent_evidence(),
    )
    detector = relogin.StateDetector(page)

    assert detector._is_authenticated() is True
    assert detector.detect_state() == relogin.AuthState.AUTHENTICATED


def test_body_or_create_button_can_prove_same_consent_state():
    page = FakePage(
        url="https://hiring.amazon.ca/application/ca/#/consent",
        evidence=_live_consent_evidence(
            titleConsent=False,
            bodyConsent=True,
        ),
    )
    assert relogin.StateDetector(page)._is_authenticated() is True

    page.evidence = _live_consent_evidence(
        titleConsent=False,
        createVisible=True,
    )
    assert relogin.StateDetector(page)._is_authenticated() is True


def test_application_url_without_consent_route_is_not_authenticated():
    page = FakePage(
        url="https://hiring.amazon.ca/application/ca/",
        evidence=_live_consent_evidence(routeConsent=False),
    )
    detector = relogin.StateDetector(page)

    assert detector._is_authenticated() is False


def test_consent_route_without_layout_is_not_authenticated():
    page = FakePage(
        url="https://hiring.amazon.ca/application/ca/#/consent",
        evidence=_live_consent_evidence(layoutVisible=False),
    )
    detector = relogin.StateDetector(page)

    assert detector._is_authenticated() is False


def test_application_ui_without_token_structure_is_not_authenticated():
    page = FakePage(
        url="https://hiring.amazon.ca/application/ca/#/consent",
        evidence=_live_consent_evidence(tokenStructure=False),
    )
    detector = relogin.StateDetector(page)

    assert detector._is_authenticated() is False


def test_visible_login_control_overrides_protected_page_evidence():
    page = FakePage(
        url="https://hiring.amazon.ca/application/ca/#/consent",
        evidence=_live_consent_evidence(loginVisible=True),
    )
    detector = relogin.StateDetector(page)

    assert detector._is_authenticated() is False


def test_public_job_search_url_still_does_not_count_as_authenticated():
    page = FakePage(
        url="https://hiring.amazon.ca/app#/jobSearch",
        evidence=_live_consent_evidence(),
    )
    detector = relogin.StateDetector(page)

    assert detector._is_authenticated() is False

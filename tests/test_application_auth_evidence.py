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



def test_protected_application_consent_with_token_structure_counts_as_authenticated():
    page = FakePage(
        url="https://hiring.amazon.ca/application/ca/#/consent",
        body_text="By applying, you confirm that:",
        evidence={"consentMounted": True, "tokenStructure": True},
    )
    detector = relogin.StateDetector(page)

    assert detector._is_authenticated() is True
    assert detector.detect_state() == relogin.AuthState.AUTHENTICATED


def test_application_url_without_protected_ui_is_not_authenticated():
    page = FakePage(
        url="https://hiring.amazon.ca/application/ca/",
        evidence={"consentMounted": False, "tokenStructure": True},
    )
    detector = relogin.StateDetector(page)

    assert detector._is_authenticated() is False


def test_application_ui_without_token_structure_is_not_authenticated():
    page = FakePage(
        url="https://hiring.amazon.ca/application/ca/#/consent",
        body_text="By applying, you confirm that:",
        evidence={"consentMounted": True, "tokenStructure": False},
    )
    detector = relogin.StateDetector(page)

    assert detector._is_authenticated() is False


def test_public_job_search_url_still_does_not_count_as_authenticated():
    page = FakePage(
        url="https://hiring.amazon.ca/app#/jobSearch",
        evidence={"consentMounted": True, "tokenStructure": True},
    )
    detector = relogin.StateDetector(page)

    assert detector._is_authenticated() is False

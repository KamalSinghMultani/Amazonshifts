from __future__ import annotations

import auth_evidence


class FakePage:
    url = "https://hiring.amazon.ca/app#/jobSearch"

    def evaluate(self, _script):
        return {
            "path": "/app#/jobSearch",
            "title": "Amazon Hiring",
            "visible_test_ids": ["account-menu", "jobResultContainer"],
            "visible_element_ids": ["root"],
            "local_storage_keys": ["candidate-session", "locale"],
            "session_storage_keys": ["flow"],
            "login_controls_visible": False,
            "account_text_marker_visible": True,
            "application_action_visible": False,
        }


class FakeContext:
    def cookies(self, _urls):
        return [
            {"name": "session-cookie", "value": "SECRET", "domain": ".hiring.amazon.ca"},
            {"name": "other-cookie", "value": "OTHERSECRET", "domain": ".example.com"},
        ]


def test_collect_returns_structure_and_cookie_names_only():
    evidence = auth_evidence.collect(FakePage(), FakeContext(), "https://hiring.amazon.ca")

    assert evidence["host"] == "hiring.amazon.ca"
    assert evidence["title"] == "Amazon Hiring"
    assert evidence["visible_test_ids"] == ["account-menu", "jobResultContainer"]
    assert evidence["local_storage_keys"] == ["candidate-session", "locale"]
    assert evidence["cookie_names"] == ["session-cookie"]
    assert "SECRET" not in repr(evidence)
    assert "OTHERSECRET" not in repr(evidence)


def test_collect_never_copies_storage_values():
    evidence = auth_evidence.collect(FakePage(), FakeContext(), "https://hiring.amazon.ca")

    assert set(evidence) >= {
        "local_storage_keys",
        "session_storage_keys",
        "cookie_names",
        "login_controls_visible",
    }

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import watcher_v4


class FakeContext:
    def __init__(self):
        self.cookies_added = []
        self.saved_to = None

    def add_cookies(self, cookies):
        self.cookies_added.extend(cookies)

    def storage_state(self, path=None):
        self.saved_to = path
        return {}


class FakePage:
    def __init__(self):
        self.url = "https://hiring.amazon.ca/app#/jobSearch"
        self.evaluated = []
        self.gotos = []

    def goto(self, url, **_kwargs):
        self.gotos.append(url)
        self.url = url

    def evaluate(self, script, arg):
        self.evaluated.append((script, arg))


class FakeTokenSource:
    def __init__(self):
        self.refreshes = 0

    def refresh(self):
        self.refreshes += 1


def test_verified_state_import_copies_cookies_and_canada_local_storage(tmp_path):
    refresh_file = tmp_path / "refresh.json"
    refresh_file.write_text(json.dumps({
        "cookies": [{"name": "session", "value": "x", "domain": ".amazon.ca", "path": "/"}],
        "origins": [
            {
                "origin": "https://hiring.amazon.ca",
                "localStorage": [
                    {"name": "sessionToken", "value": "token-value"},
                    {"name": "country", "value": "Canada"},
                ],
            },
            {
                "origin": "https://hiring.amazon.com",
                "localStorage": [{"name": "sessionToken", "value": "wrong-country"}],
            },
        ],
    }), "utf-8")

    watcher = object.__new__(watcher_v4.AutoSessionWatcher)
    watcher.refresh_state_path = refresh_file
    watcher.context = FakeContext()
    watcher.page = FakePage()
    watcher.token_source = FakeTokenSource()
    watcher.cfg = {
        "site": {
            "base_url": "https://hiring.amazon.ca",
            "job_search_url": "https://hiring.amazon.ca/app#/jobSearch",
        },
        "browser": {"storage_state": str(tmp_path / "auth_state.json")},
    }

    watcher._apply_refreshed_state()

    assert len(watcher.context.cookies_added) == 1
    assert watcher.page.evaluated, "Canada localStorage should be injected"
    injected = watcher.page.evaluated[0][1]
    assert {item["name"] for item in injected} == {"sessionToken", "country"}
    assert all(item["value"] != "wrong-country" for item in injected)
    assert watcher.token_source.refreshes == 1
    assert watcher.context.saved_to == str(tmp_path / "auth_state.json")


def test_origin_normalization_matches_only_configured_country():
    assert watcher_v4._origin("https://hiring.amazon.ca/app#/jobSearch") == "https://hiring.amazon.ca"
    assert watcher_v4._origin("https://hiring.amazon.com/app") == "https://hiring.amazon.com"
    assert watcher_v4._origin("https://hiring.amazon.ca") != watcher_v4._origin("https://hiring.amazon.com")

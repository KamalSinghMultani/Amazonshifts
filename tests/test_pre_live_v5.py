from __future__ import annotations

import inspect
from datetime import datetime

import fast_hold
import real_hold_test
import session_refresh
import site_selectors
import watcher_v4
import watcher_v5


def test_v5_extends_v4_instead_of_replacing_original_stack():
    assert issubclass(watcher_v5.PreLiveWatcher, watcher_v4.AutoSessionWatcher)


def test_fast_hold_is_browser_driven_and_passively_observes_backend():
    source = inspect.getsource(fast_hold)
    assert "SoftReserveObserver" in source
    assert "page.goto" in source
    assert "Create Application" in source
    assert "context.request" not in source
    assert "candidate-application/ds/create-application" not in source
    assert "candidate-application/update-application" not in source


def test_fast_hold_can_finish_from_backend_confirmation_without_ui_banner(monkeypatch):
    class Observer:
        def __init__(self, _page, _schedule):
            self.confirmed = False
            self.schedule_id = "SCH-1"
            self.expiration = "later"

        def __enter__(self):
            self.confirmed = True
            return self

        def __exit__(self, *_args):
            return None

        def detail(self):
            return "backend soft reserve confirmed"

    class Page:
        url = "https://hiring.amazon.ca/application/ca/"
        context = None

        def goto(self, url, **_kwargs):
            self.url = url

    monkeypatch.setattr(fast_hold.hold_verify, "SoftReserveObserver", Observer)
    result, detail = fast_hold.hold(
        Page(),
        "https://hiring.amazon.ca/application/ca/?jobId=J&scheduleId=SCH-1",
        "SCH-1",
        base_url="https://hiring.amazon.ca",
        stop_before_submit=False,
        timeout_ms=1000,
    )

    assert result.status == site_selectors.CONFIRMED
    assert result.held is True
    assert "backend" in detail
    assert any(name == "backend reserve confirmed" for name, _ms in result.timings)


def test_session_refresh_captures_failed_background_login_page():
    source = inspect.getsource(session_refresh.run)
    assert "_capture_failure" in source
    assert "failure_capture=captured" in source
    assert "failure screenshot" in inspect.getsource(session_refresh._with_capture)


def test_real_test_requires_explicit_ack_before_loading_config():
    assert real_hold_test.main([]) == 2


def test_real_test_config_is_live_but_time_bounded(monkeypatch):
    cfg = {
        "dry_run": True,
        "hold": {
            "enabled": False,
            "direct_apply": False,
            "stop_before_submit": True,
            "max_per_poll": 9,
        },
        "filters": {},
        "browser": {
            "storage_state": "auth_state.json",
            "user_data_dir": "browser_profile",
        },
    }
    monkeypatch.setattr(real_hold_test, "load_config", lambda _path: cfg)
    out = real_hold_test._prepare_cfg("config.yaml", 60, real_hold_test.Path("verified.json"))

    assert out["dry_run"] is False
    assert out["hold"]["enabled"] is True
    assert out["hold"]["direct_apply"] is True
    assert out["hold"]["stop_before_submit"] is False
    assert out["hold"]["max_per_poll"] == 1
    assert out["browser"]["storage_state"] == "verified.json"
    assert out["browser"]["user_data_dir"] is None
    until = datetime.fromisoformat(out["filters"]["accept_everything_until"])
    delta = (until - datetime.now()).total_seconds()
    assert 3500 < delta <= 3601


def test_real_test_stops_after_uncertain_or_confirmed_commit():
    source = inspect.getsource(real_hold_test.RealHoldTestWatcher._hold)
    assert "result.held" in source
    assert "site_selectors.UNCERTAIN" in source
    assert "self.stop_event.set()" in source


def test_v5_cleans_up_background_session_worker_on_exit():
    source = inspect.getsource(watcher_v5.PreLiveWatcher.run)
    assert "_stop_session_worker" in source

from __future__ import annotations

import inspect
from datetime import datetime

import config as config_mod
import fast_hold
import real_hold_test
import session_refresh
import site_selectors
import watcher_v4
import watcher_v5


def test_v5_extends_v4_instead_of_replacing_original_stack():
    assert issubclass(watcher_v5.PreLiveWatcher, watcher_v4.AutoSessionWatcher)


def test_v5_primes_persistent_context_before_poll_loop():
    source = inspect.getsource(watcher_v5.PreLiveWatcher._start_api_mode)
    assert "self.context.add_cookies" in source
    assert "localStorage.setItem" in source
    assert "super()._start_api_mode" in source
    assert "browser_cfg.get(\"user_data_dir\")" in source


def test_v5_prewarms_a_dedicated_application_page_without_job_or_schedule_ids():
    class Page:
        def __init__(self):
            self.visited = []

        def is_closed(self):
            return False

        def goto(self, url, **kwargs):
            self.visited.append((url, kwargs))

    class Context:
        def __init__(self):
            self.created = []

        def new_page(self):
            page = Page()
            self.created.append(page)
            return page

    watcher = watcher_v5.PreLiveWatcher.__new__(watcher_v5.PreLiveWatcher)
    watcher.cfg = {
        "site": {"base_url": "https://hiring.amazon.ca"},
        "browser": {"nav_timeout_ms": 30000},
        "hold": {"prewarm_application": True},
    }
    watcher.context = Context()
    watcher.hold_page = None

    assert watcher._prewarm_application_page() is True
    assert watcher.hold_page is watcher.context.created[0]
    target, kwargs = watcher.hold_page.visited[0]
    assert target == "https://hiring.amazon.ca/application/ca/"
    assert "jobId" not in target and "scheduleId" not in target
    assert kwargs["wait_until"] == "commit"


def test_v5_cold_prewarm_waits_for_and_clears_late_overlays(monkeypatch):
    class Page:
        def __init__(self):
            self.waits = []

        def is_closed(self):
            return False

        def goto(self, _url, **_kwargs):
            return None

        def wait_for_timeout(self, milliseconds):
            self.waits.append(milliseconds)

    class Context:
        def __init__(self):
            self.page = Page()

        def new_page(self):
            return self.page

    dismissed = []

    def clear(page, **kwargs):
        dismissed.append((page, kwargs))
        return ["cookie consent", "banner"]

    monkeypatch.setattr(watcher_v5.site_selectors, "dismiss_overlays", clear)
    watcher = watcher_v5.PreLiveWatcher.__new__(watcher_v5.PreLiveWatcher)
    watcher.cfg = {
        "site": {"base_url": "https://hiring.amazon.ca"},
        "browser": {"nav_timeout_ms": 30000, "action_timeout_ms": 10000},
        "hold": {
            "prewarm_application": True,
            "prewarm_overlay_settle_ms": 4321,
        },
    }
    watcher.context = Context()
    watcher.hold_page = None

    assert watcher._prewarm_application_page() is True
    assert watcher.hold_page.waits == [4321]
    assert dismissed == [
        (watcher.hold_page, {"timeout_ms": 750, "rounds": 4})
    ]


def test_latency_first_defaults_prewarm_and_disable_compatibility_fallback():
    cfg = config_mod.load_config(config_mod.Path(__file__).resolve().parent.parent / "config.yaml")
    assert cfg["hold"]["prewarm_application"] is True
    assert cfg["hold"]["prewarm_overlay_settle_ms"] == 4500
    assert cfg["hold"]["compatibility_fallback"] is False
    assert cfg["hold"]["auto_integrity_agree"] is True
    assert cfg["hold"]["auto_accept_identity_consent_and_start"] is True


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


def test_fast_hold_integrity_agree_is_explicit_opt_in_and_reservation_only():
    signature = inspect.signature(fast_hold.hold)
    assert signature.parameters["auto_integrity_agree"].default is False
    assert signature.parameters["auto_accept_identity_consent_and_start"].default is False

    source = inspect.getsource(fast_hold.hold)
    assert "integrity-notice-agree-button" in inspect.getsource(fast_hold)
    assert "application-integrity-notice" in inspect.getsource(fast_hold)
    assert "if auto_integrity_agree:" in source
    assert "page.locator(INTEGRITY_AGREE).first.click" in source
    assert "integrity agree clicked" in source
    assert "backend reserve confirmed" in source
    assert "later application fields will not be touched" in source


def test_fast_hold_detects_unavailable_after_integrity():
    class Page:
        def inner_text(self, _selector):
            return "Sorry, this shift is no longer available. Please choose another schedule."

    detail = fast_hold._availability_failure(Page())
    assert "shift is no longer available" in detail.lower()


def test_fast_hold_does_not_capture_before_create_click():
    source = inspect.getsource(fast_hold.hold)
    start = source.index("if create_actionable and not create_clicked:")
    end = source.index("if create_clicked:", start)
    critical = source[start:end]
    click = critical.index("page.locator(CREATE).first.click")
    before_click = critical[:click]
    assert "page.screenshot" not in before_click
    assert "failure_capture.capture" not in before_click


def test_v5_passes_integrity_settings_and_skips_fallback_after_agree():
    source = inspect.getsource(watcher_v5.PreLiveWatcher._direct_hold)
    assert "manual_integrity_wait" in source
    assert "manual_integrity_timeout_ms" in source
    assert "auto_integrity_agree" in source
    assert "auto_accept_identity_consent_and_start" in source
    assert "integrity agree clicked" in source
    assert "skipping compatibility fallback" in source
    assert "compatibility fallback disabled for latency" in source


def test_session_refresh_captures_failed_background_login_page():
    source = inspect.getsource(session_refresh.run)
    assert "_capture_failure" in source
    assert "failure_capture=captured" in source
    assert "failure screenshot" in inspect.getsource(session_refresh._with_capture)


def test_real_test_requires_explicit_ack_before_loading_config():
    assert real_hold_test.main([]) == 2


def test_real_test_config_is_live_but_time_bounded_and_isolated(monkeypatch):
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
            "headless": True,
        },
        "state": {
            "path": "state/seen_shifts.json",
            "detections_path": "state/detections.jsonl",
        },
    }
    monkeypatch.setattr(real_hold_test, "load_config", lambda _path: cfg)
    out = real_hold_test._prepare_cfg("config.yaml", 60, real_hold_test.Path("verified.json"))

    assert out["dry_run"] is False
    assert out["hold"]["enabled"] is True
    assert out["hold"]["direct_apply"] is True
    assert out["hold"]["prewarm_application"] is True
    assert out["hold"]["compatibility_fallback"] is False
    assert out["hold"]["stop_before_submit"] is False
    assert out["hold"]["max_per_poll"] == 1
    assert out["hold"]["job_attempts"] >= 3
    assert out["hold"]["manual_integrity_wait"] is False
    assert out["hold"]["manual_integrity_timeout_ms"] == 120000
    assert out["hold"]["auto_integrity_agree"] is True
    assert out["hold"]["auto_accept_identity_consent_and_start"] is True
    assert out["browser"]["storage_state"] == "verified.json"
    assert out["browser"]["user_data_dir"] is None
    assert out["browser"]["headless"] is False
    assert out["state"]["path"] == "state/real_hold_test_seen.json"
    assert out["state"]["detections_path"] == "state/real_hold_test_detections.jsonl"
    until = datetime.fromisoformat(out["filters"]["accept_everything_until"])
    delta = (until - datetime.now()).total_seconds()
    assert 3500 < delta <= 3601


def test_real_test_background_workers_are_repointed_to_runtime_config():
    source = inspect.getsource(real_hold_test.main)
    writer = inspect.getsource(real_hold_test._write_runtime_config)
    assert "watcher.config_path = str(runtime_config)" in source
    assert "real_hold_test_runtime.yaml" in writer
    assert "hot_windows_parsed" in writer
    assert "yaml.safe_dump" in writer


def test_real_test_attaches_sanitized_research_trace_before_page_startup():
    source = inspect.getsource(real_hold_test.RealHoldTestWatcher._start_api_mode)
    assert "SafeResearchTrace" in source
    assert source.index(".start()") < source.index("super()._start_api_mode")
    assert "research_trace.stop()" in inspect.getsource(
        real_hold_test.RealHoldTestWatcher.run
    )


def test_real_test_explains_auto_reservation_and_retry_boundary():
    source = inspect.getsource(real_hold_test.main)
    assert "Create Application -> I Agree -> reserve result" in source
    assert "Explicit unavailable -> try next ranked schedule" in source
    assert "Later personal-info/documents/assessment/identity steps are never filled or clicked" in source
    assert "DOM timing markers" in source


def test_real_test_stops_on_confirmed_or_uncertain_but_retries_explicit_unavailable():
    source = inspect.getsource(real_hold_test.RealHoldTestWatcher._hold)
    assert "site_selectors.UNCERTAIN" in source
    assert "site_selectors.IDENTITY_VERIFICATION_REQUIRED" in source
    assert "self.stop_event.set()" in source
    assert "schedule explicitly unavailable after I Agree" in source
    assert "trying next ranked schedule" in source


def test_v5_cleans_up_background_session_worker_on_exit():
    source = inspect.getsource(watcher_v5.PreLiveWatcher.run)
    assert "_stop_session_worker" in source

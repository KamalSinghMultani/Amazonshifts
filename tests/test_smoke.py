"""Smoke tests. No browser and no network required.

Run: python -m pytest -q
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import api_client
import auth_token
import browser_launch
import config as config_mod
import doctor
import drop_report
import otp_mail
import login_flow as relogin
import schedules as schedules_mod
import site_selectors
from notifier import TelegramNotifier
from shift_matcher import Shift, ShiftMatcher, ShiftRanker, warehouse_type
from state_store import StateStore


# ── Shift ───────────────────────────────────────────────────────────────────
def test_shift_normalizes_whitespace_and_pay():
    shift = Shift(title="  Warehouse   Associate\n", location=" Brampton ", pay_rate="$18.50/hr")
    assert shift.title == "Warehouse Associate"
    assert shift.location == "Brampton"
    assert shift.pay_rate == 18.50


def test_stable_id_prefers_site_id():
    assert Shift(id="ABC123", title="x").stable_id == "id:ABC123"


def test_stable_id_is_stable_and_distinct_without_an_id():
    a = Shift(title="Sorter", location="YYZ", schedule="Mon 8-4")
    b = Shift(title="sorter", location="YYZ", schedule="Mon 8-4")  # case differs only
    c = Shift(title="Sorter", location="YOW", schedule="Mon 8-4")
    assert a.stable_id == b.stable_id
    assert a.stable_id != c.stable_id
    assert a.stable_id.startswith("h:")


def test_pay_rate_junk_becomes_none():
    assert Shift(pay_rate="competitive").pay_rate is None
    assert Shift(pay_rate=None).pay_rate is None


# ── ShiftMatcher ────────────────────────────────────────────────────────────
def test_empty_filters_match_everything():
    assert ShiftMatcher({}).matches(Shift(title="anything"))[0]


def test_include_and_exclude_titles():
    matcher = ShiftMatcher({"include_titles": ["warehouse"], "exclude_titles": ["seasonal"]})
    assert matcher.matches(Shift(title="Warehouse Associate"))[0]
    assert not matcher.matches(Shift(title="Delivery Driver"))[0]
    assert not matcher.matches(Shift(title="Seasonal Warehouse Associate"))[0]


def test_exclude_beats_include():
    matcher = ShiftMatcher({"include_titles": ["warehouse"], "exclude_titles": ["warehouse"]})
    matched, reason = matcher.matches(Shift(title="Warehouse"))
    assert not matched and "excluded" in reason


def test_min_pay_rate():
    matcher = ShiftMatcher({"min_pay_rate": 20})
    assert matcher.matches(Shift(title="a", pay_rate=22))[0]
    assert not matcher.matches(Shift(title="a", pay_rate=19.5))[0]
    # No pay data must not silently pass a pay filter.
    assert not matcher.matches(Shift(title="a"))[0]


def test_location_and_schedule_filters():
    matcher = ShiftMatcher({"include_locations": ["brampton"], "exclude_schedules": ["night"]})
    assert matcher.matches(Shift(title="a", location="Brampton, ON", schedule="Day"))[0]
    assert not matcher.matches(Shift(title="a", location="Ottawa", schedule="Day"))[0]
    assert not matcher.matches(Shift(title="a", location="Brampton", schedule="Night shift"))[0]


# ── StateStore ──────────────────────────────────────────────────────────────
def test_state_store_persists_across_instances(tmp_path):
    path = tmp_path / "state" / "seen.json"
    store = StateStore(path)
    assert not store.has_seen("id:1")
    store.mark_seen("id:1", "Sorter")
    store.save()

    assert StateStore(path).has_seen("id:1")


def test_state_store_expires_entries(tmp_path):
    path = tmp_path / "seen.json"
    store = StateStore(path, ttl_hours=1)
    store.mark_seen("id:1")
    store._seen["id:1"]["ts"] = time.time() - 7200  # 2h ago
    assert not store.has_seen("id:1")


def test_state_store_survives_a_corrupt_file(tmp_path):
    path = tmp_path / "seen.json"
    path.write_text("{not json at all", "utf-8")
    store = StateStore(path)  # must not raise
    assert len(store) == 0
    store.mark_seen("id:1")
    store.save()
    assert json.loads(path.read_text())["seen"]["id:1"]


# ── api_client ──────────────────────────────────────────────────────────────
def test_dig_walks_dicts_and_list_indexes():
    payload = {"data": {"results": [{"cards": [1, 2]}]}}
    assert api_client.dig(payload, "data.results.0.cards") == [1, 2]


def test_dig_returns_none_instead_of_raising():
    assert api_client.dig({"a": 1}, "a.b.c") is None
    assert api_client.dig({}, "missing") is None
    assert api_client.dig({"a": [1]}, "a.9") is None


def test_parse_shifts_maps_fields_and_builds_urls():
    payload = {
        "data": {
            "jobCards": [
                {
                    "jobId": "JOB-1",
                    "jobTitle": "Warehouse Associate",
                    "locationName": "Brampton, ON",
                    "scheduleText": "Mon-Fri 08:00-16:30",
                    "totalPayRateMin": 18.5,
                }
            ]
        }
    }
    shifts = api_client.parse_shifts(
        payload,
        "data.jobCards",
        {
            "id": "jobId",
            "title": "jobTitle",
            "location": "locationName",
            "schedule": "scheduleText",
            "pay_rate": "totalPayRateMin",
            "url": None,
        },
        url_template="https://hiring.amazon.ca/app#/jobDetail?jobId={id}",
    )
    assert len(shifts) == 1
    shift = shifts[0]
    assert shift.id == "JOB-1"
    assert shift.title == "Warehouse Associate"
    assert shift.pay_rate == 18.5
    assert shift.url.endswith("jobId=JOB-1")
    assert shift.raw["jobId"] == "JOB-1"  # raw payload kept for debugging


def test_parse_shifts_handles_a_schema_change_without_crashing():
    assert api_client.parse_shifts({"data": {}}, "data.jobCards", {"id": "jobId"}) == []
    assert api_client.parse_shifts({"data": {"jobCards": {}}}, "data.jobCards", {"id": "x"}) == []


def test_parse_shifts_ignores_unknown_field_map_keys():
    shifts = api_client.parse_shifts(
        {"cards": [{"jobTitle": "A", "bogus": "B"}]},
        "cards",
        {"title": "jobTitle", "not_a_shift_field": "bogus"},
    )
    assert shifts[0].title == "A"


# ── config ──────────────────────────────────────────────────────────────────
def test_shipped_config_loads_and_validates():
    cfg = config_mod.load_config(Path(__file__).resolve().parent.parent / "config.yaml")
    assert cfg["polling"]["mode"] in ("dom", "api")
    assert cfg["state"]["path"]


def test_a_live_config_must_have_filters_and_a_hold_cap():
    """config.yaml is now deliberately live, so "safe by default" no longer
    fits it. What still has to hold: a live watcher that matches everything
    would auto-create applications on shifts across the whole country, and one
    that holds several per poll multiplies the clicks for no benefit — you can
    only work one shift."""
    cfg = config_mod.load_config(Path(__file__).resolve().parent.parent / "config.yaml")
    if cfg["dry_run"]:
        return  # nothing to guard

    filters = cfg["filters"]
    assert filters.get("include_titles"), "live + no title filter would hold anything"
    assert filters.get("include_locations"), "live + no location filter is country-wide"
    assert cfg["hold"]["max_per_poll"] == 1
    assert cfg["notifications"]["max_alerts_per_poll"], "live runs need an alert cap"


def test_defaults_fill_in_missing_sections(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text("dry_run: false\n", "utf-8")
    cfg = config_mod.load_config(path)
    assert cfg["dry_run"] is False
    assert cfg["polling"]["interval_seconds"] == 45  # from DEFAULTS


def test_api_mode_without_an_endpoint_is_rejected(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text("polling:\n  mode: api\n", "utf-8")
    with pytest.raises(ValueError, match="endpoint_url"):
        config_mod.load_config(path)


def test_absurd_poll_interval_is_rejected(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text("polling:\n  interval_seconds: 1\n", "utf-8")
    with pytest.raises(ValueError, match="interval_seconds"):
        config_mod.load_config(path)


def test_bad_mode_is_rejected(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text("polling:\n  mode: telepathy\n", "utf-8")
    with pytest.raises(ValueError, match="polling.mode"):
        config_mod.load_config(path)


def test_dotenv_does_not_override_real_env(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('TELEGRAM_BOT_TOKEN="from-file"\nTELEGRAM_CHAT_ID=123\n', "utf-8")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "from-shell")
    config_mod.load_dotenv(env)
    import os

    assert os.environ["TELEGRAM_BOT_TOKEN"] == "from-shell"
    assert os.environ["TELEGRAM_CHAT_ID"] == "123"


# ── browser_launch ──────────────────────────────────────────────────────────
class FakeContext:
    def __init__(self):
        self.init_scripts = []
        self.closed = False
        self.pages = []

    def add_init_script(self, script):
        self.init_scripts.append(script)

    def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self):
        self.closed = False
        self.new_context_kwargs = None

    def new_context(self, **kwargs):
        self.new_context_kwargs = kwargs
        return FakeContext()

    def close(self):
        self.closed = True


class FakeChromium:
    def __init__(self):
        self.launch_kwargs = None
        self.persistent_kwargs = None
        self.persistent_dir = None
        self.browser = FakeBrowser()

    def launch(self, **kwargs):
        self.launch_kwargs = kwargs
        return self.browser

    def launch_persistent_context(self, user_data_dir, **kwargs):
        self.persistent_dir = user_data_dir
        self.persistent_kwargs = kwargs
        return FakeContext()


class FakePlaywright:
    def __init__(self):
        self.chromium = FakeChromium()


def test_launch_uses_a_fresh_context_when_no_profile_is_configured():
    pw = FakePlaywright()
    browser, context = browser_launch.launch_context(
        pw, {"user_data_dir": None, "headless": True}, storage_state="auth_state.json"
    )
    assert browser is pw.chromium.browser
    assert pw.chromium.persistent_dir is None
    assert browser.new_context_kwargs["storage_state"] == "auth_state.json"


def test_launch_uses_a_persistent_profile_when_configured(tmp_path):
    """A persistent profile is what stops Amazon re-challenging every login."""
    pw = FakePlaywright()
    profile = tmp_path / "browser_profile"
    browser, context = browser_launch.launch_context(
        pw, {"user_data_dir": str(profile), "headless": False}
    )
    assert browser is None, "persistent context owns the browser process"
    assert pw.chromium.persistent_dir == str(profile)
    assert profile.exists(), "profile directory should be created"


def test_stealth_removes_automation_flags_and_webdriver():
    pw = FakePlaywright()
    _, context = browser_launch.launch_context(pw, {"stealth": True, "user_data_dir": None})
    kwargs = pw.chromium.launch_kwargs
    assert "--disable-blink-features=AutomationControlled" in kwargs["args"]
    assert "--enable-automation" in kwargs["ignore_default_args"]
    assert any("webdriver" in s for s in context.init_scripts)


def test_stealth_can_be_turned_off():
    pw = FakePlaywright()
    _, context = browser_launch.launch_context(pw, {"stealth": False, "user_data_dir": None})
    assert "args" not in pw.chromium.launch_kwargs
    assert context.init_scripts == []


def test_channel_selects_the_real_installed_browser():
    pw = FakePlaywright()
    browser_launch.launch_context(pw, {"channel": "chrome", "user_data_dir": None})
    assert pw.chromium.launch_kwargs["channel"] == "chrome"


def test_no_channel_key_means_bundled_chromium():
    pw = FakePlaywright()
    browser_launch.launch_context(pw, {"channel": None, "user_data_dir": None})
    assert "channel" not in pw.chromium.launch_kwargs


def test_close_context_closes_whichever_owns_the_process():
    browser, context = FakeBrowser(), FakeContext()
    browser_launch.close_context(browser, context)
    assert browser.closed and not context.closed

    context2 = FakeContext()
    browser_launch.close_context(None, context2)  # persistent case
    assert context2.closed


def test_headed_mode_needs_no_user_agent_override():
    pw = FakePlaywright()
    assert browser_launch.resolve_user_agent(pw, {}, headless=False) is None


def test_explicit_user_agent_always_wins():
    pw = FakePlaywright()
    assert browser_launch.resolve_user_agent(pw, {"user_agent": "custom"}, True) == "custom"


def test_headless_user_agent_strips_the_headless_marker(monkeypatch):
    """Regression: 'HeadlessChrome' in the UA header gets you a CloudFront
    403 'Request blocked' page instead of the site."""
    ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) HeadlessChrome/151.0.0.0 Safari/537.36")

    class Page:
        def evaluate(self, _script):
            return ua

    class Browser:
        def new_page(self):
            return Page()

        def close(self):
            pass

    pw = FakePlaywright()
    monkeypatch.setattr(pw.chromium, "launch", lambda **kw: Browser())
    fixed = browser_launch.resolve_user_agent(pw, {}, headless=True)
    assert "Headless" not in fixed
    assert "Chrome/151.0.0.0" in fixed


def test_user_agent_probe_failure_is_not_fatal(monkeypatch):
    pw = FakePlaywright()

    def boom(**kwargs):
        raise RuntimeError("no browser")

    monkeypatch.setattr(pw.chromium, "launch", boom)
    assert browser_launch.resolve_user_agent(pw, {}, headless=True) is None


# ── WAF / blocked-page detection ────────────────────────────────────────────
class TextPage:
    def __init__(self, text, url="https://hiring.amazon.ca/app#/jobSearch", captcha=False):
        self._text, self.url, self._captcha = text, url, captcha

    def inner_text(self, _selector):
        return self._text

    def locator(self, _selector):
        return FakeField(present=self._captcha, visible=self._captcha)


def test_cloudfront_block_is_detected_not_read_as_zero_shifts():
    """The block page returns HTTP 200 with no job cards. Reporting that as
    'no shifts' would be silent, permanent failure."""
    page = TextPage(
        "403 ERROR\nThe request could not be satisfied.\nRequest blocked.\n"
        "Generated by cloudfront (CloudFront)"
    )
    state, detail = site_selectors.page_state(page)
    assert state == "blocked"
    assert detail


def test_expired_token_is_stale_not_blocked():
    """A reload refreshes the token, so this must not trip the breaker."""
    state, _ = site_selectors.page_state(TextPage("Token expired. Try refreshing the browser."))
    assert state == "stale"


def test_login_redirect_is_detected_from_the_url():
    page = TextPage("whatever", url="https://auth.hiring.amazon.com/login?foo=1")
    state, _ = site_selectors.page_state(page)
    assert state == "login"


def test_a_normal_page_is_ok():
    state, _ = site_selectors.page_state(TextPage("Jobs at Amazon\nWarehouse Associate"))
    assert state == "ok"


def test_visible_captcha_is_detected():
    state, _ = site_selectors.page_state(TextPage("Jobs at Amazon", captcha=True))
    assert state == "captcha"


def test_hidden_captcha_modal_is_not_a_captcha():
    """The modal ships in the DOM on every page load — only visibility counts."""
    state, _ = site_selectors.page_state(TextPage("Jobs at Amazon", captcha=False))
    assert state == "ok"


def test_card_selectors_are_configured_from_the_live_site():
    """Captured from real job cards on hiring.amazon.com."""
    assert site_selectors.SELECTORS["job_card"] == "[data-test-id='JobCard']"
    assert site_selectors.SELECTORS["card_pay"] == "[data-test-id='jobCardPayRateText']"
    for name in ("job_card", "card_title", "card_pay", "card_schedule", "card_location"):
        assert name not in site_selectors.unconfigured(), name


def test_first_two_hold_steps_are_confirmed():
    assert site_selectors.HOLD_STEPS[0][1] == ":scope"       # card is the button
    assert "jobDetailSelectScheduleButton" in site_selectors.HOLD_STEPS[1][1]


class DetailPage(TextPage):
    def __init__(self, url, marker_count=0):
        super().__init__("", url=url)
        self._marker_count = marker_count

    def locator(self, _selector):
        return FakeField(present=self._marker_count > 0)


def test_detail_page_detected_from_url():
    page = DetailPage("https://hiring.amazon.com/app#/jobDetail?jobId=JOB-US-1")
    assert site_selectors.on_detail_page(page)


def test_detail_page_detected_from_marker_when_url_is_opaque():
    assert site_selectors.on_detail_page(DetailPage("https://hiring.amazon.ca/app", 1))


def test_results_list_is_not_a_detail_page():
    assert not site_selectors.on_detail_page(
        DetailPage("https://hiring.amazon.ca/app#/jobSearch", 0)
    )


def test_hold_skips_the_card_click_when_already_on_the_detail_page(monkeypatch):
    """Regression: api mode navigates straight to the job URL, where no cards
    exist — attempting the card click there would always fail."""
    monkeypatch.setattr(site_selectors, "CREATE_APPLICATION", "#create-app")
    monkeypatch.setattr(site_selectors, "HOLD_STEPS", [site_selectors.HoldStep("open job", ":scope"), site_selectors.HoldStep("sched", "#s")])
    monkeypatch.setattr(site_selectors, "dismiss_overlays", lambda *a, **k: [])

    clicked, searched = [], []

    class Target:
        def wait_for(self, **kw):
            pass

        def click(self, **kw):
            clicked.append(True)

    class Page:
        url = "https://hiring.amazon.com/app#/jobDetail?jobId=X"

        def locator(self, sel):
            class L:
                first = Target()

                def count(self_inner):
                    return 1
            return L()

        def screenshot(self, **kw):
            pass

    monkeypatch.setattr(
        site_selectors, "find_matching_card",
        lambda *a: searched.append(True) or None,
    )
    result = site_selectors.hold_shift(Page(), Shift(id="X", title="t"))
    assert not searched, "must not hunt for a card on the detail page"
    assert result.status != site_selectors.FAILED, result.message


def test_no_results_selector_is_configured_from_the_live_site():
    """Confirmed live: <div id="jobNotFoundContainer">. Without this, 'no jobs
    posted' and 'our selectors rotted' are indistinguishable."""
    assert site_selectors.SELECTORS["no_results"] == "#jobNotFoundContainer"
    assert "no_results" not in site_selectors.unconfigured()


def test_polling_defaults_are_conservative_enough_for_the_waf():
    """The floor depends on what a poll costs. A dom poll is a full page load
    and a live test got CloudFront-blocked at ~14s between them; an api poll is
    one small JSON request, measured at ~440ms and run at 8s apart without a
    block. So the shipped numbers are only safe *for the shipped mode*."""
    cfg = config_mod.load_config(Path(__file__).resolve().parent.parent / "config.yaml")
    # api floor is measured, not guessed: 60 consecutive polls stepping down to
    # 2s apart drew no 429, no 403 and no WAF page, median 132ms.
    floor = 30 if cfg["polling"]["mode"] == "dom" else 3
    assert cfg["polling"]["interval_seconds"] >= floor
    assert cfg["polling"]["render_wait_ms"] >= 1000


def test_dom_mode_warns_when_the_api_mode_cadence_is_left_behind(caplog):
    """Switching mode back to dom without raising the interval is the easy
    mistake, and it is the one that gets you WAF-blocked."""
    cfg = config_mod._deep_merge(
        config_mod.DEFAULTS, {"polling": {"mode": "dom", "interval_seconds": 20}}
    )
    with caplog.at_level("WARNING"):
        config_mod.validate_config(cfg)
    assert any("dom mode" in r.getMessage() for r in caplog.records)


def test_shipped_config_defaults_to_settings_that_survive_login():
    cfg = config_mod.load_config(Path(__file__).resolve().parent.parent / "config.yaml")
    assert cfg["browser"]["channel"] == "chrome"
    assert cfg["browser"]["user_data_dir"]
    assert cfg["browser"]["stealth"] is True


# ── notifier ────────────────────────────────────────────────────────────────
def test_notifier_disables_itself_without_credentials(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    notifier = TelegramNotifier(enabled=True)
    assert not notifier.enabled
    # Muted, but must not raise — a missing token cannot take the watcher down.
    assert notifier.notify_shift(Shift(title="x")) is False


def test_notifier_retries_then_gives_up(monkeypatch):
    calls = []

    class FakeResponse:
        status_code = 500
        text = "boom"

    def fake_post(url, data=None, files=None, timeout=None):
        calls.append(url)
        return FakeResponse()

    monkeypatch.setattr("notifier.requests.post", fake_post)
    monkeypatch.setattr("notifier.time.sleep", lambda _s: None)
    notifier = TelegramNotifier(enabled=True, token="t", chat_id="c", max_retries=3)
    assert notifier.send_text("hi") is False
    assert len(calls) == 3


def test_notifier_sends_photo_bytes_so_retries_are_replayable(monkeypatch, tmp_path):
    """Regression: an open file handle is consumed by attempt 1 and would
    upload zero bytes on attempt 2."""
    seen = []

    class FakeResponse:
        status_code = 500
        text = ""

    def fake_post(url, data=None, files=None, timeout=None):
        seen.append(files["photo"][1])
        return FakeResponse()

    image = tmp_path / "shot.png"
    image.write_bytes(b"PNGDATA")
    monkeypatch.setattr("notifier.requests.post", fake_post)
    monkeypatch.setattr("notifier.time.sleep", lambda _s: None)

    TelegramNotifier(enabled=True, token="t", chat_id="c", max_retries=2).send_photo(image)
    assert seen == [b"PNGDATA", b"PNGDATA"]


def test_notify_shift_escapes_html():
    notifier = TelegramNotifier(enabled=False)
    sent = []
    notifier.send_text = lambda text: sent.append(text) or True
    notifier.notify_shift(Shift(title="<script>x</script>", location="A & B"))
    assert "&lt;script&gt;" in sent[0]
    assert "A &amp; B" in sent[0]


# ── site_selectors ──────────────────────────────────────────────────────────
def test_module_is_not_named_selectors():
    """Regression: a module named `selectors.py` shadows the stdlib module
    asyncio (and therefore Playwright) imports, crashing at startup."""
    assert Path(site_selectors.__file__).name == "site_selectors.py"
    import selectors as stdlib_selectors

    assert hasattr(stdlib_selectors, "DefaultSelector")


def test_every_selector_is_captured_from_the_live_site():
    """All of them are confirmed against hiring.amazon.* now, including the
    apply path: card -> Select schedule -> Apply -> the application page."""
    assert site_selectors.unconfigured() == []
    assert site_selectors.selectors_ready()
    assert site_selectors.detection_ready()

    labels = [step.label for step in site_selectors.HOLD_STEPS]
    assert labels == [
        "open job", "select schedule", "pick a shift", "open the consent screen",
    ]
    assert "ScheduleCardSelectScheduleLink" in {s.label: s.selector for s in site_selectors.HOLD_STEPS}["pick a shift"]


def test_creating_the_application_is_not_one_of_the_automatic_steps():
    """Create Application accepts the age and drug-test declarations and is
    what actually reserves the slot. It stays behind stop_before_submit, so a
    default run can never commit you to anything."""
    step_selectors = [step.selector for step in site_selectors.HOLD_STEPS]
    assert site_selectors.CREATE_APPLICATION not in step_selectors
    assert not any("Create Application" in s for s in step_selectors)
    assert "Create Application" in site_selectors.CREATE_APPLICATION


def test_detection_stays_ready_when_a_hold_selector_rots(monkeypatch):
    """Amazon will change these eventually. When a hold selector goes stale,
    detection and alerting must keep working — losing the clicks is bad,
    losing the alerts too would be worse."""
    monkeypatch.setattr(site_selectors, "CREATE_APPLICATION", site_selectors.TODO)
    assert site_selectors.detection_ready()
    assert not site_selectors.selectors_ready()
    assert "CREATE_APPLICATION" in site_selectors.unconfigured_hold()


def test_detection_can_be_ready_while_holding_is_not():
    """Regression: the watcher used to refuse to start in dom mode unless every
    selector was filled in, including the apply-flow steps that can only be
    captured by submitting a real application. That blocked the dry-run period
    you are supposed to do *first*."""
    assert site_selectors.detection_ready()
    assert site_selectors.unconfigured() == (
        site_selectors.unconfigured_detection() + site_selectors.unconfigured_hold()
    )


# ── DOM fakes, to test card matching without a browser ──────────────────────
class FakeField:
    def __init__(self, text: str = "", present: bool = True, href: str | None = None,
                 visible: bool = False):
        self._text, self._present, self._href = text, present, href
        self._visible = visible

    @property
    def first(self):
        return self

    def is_visible(self):
        return self._visible

    def count(self):
        return 1 if self._present else 0

    def inner_text(self, timeout=None):
        return self._text

    def get_attribute(self, name, timeout=None):
        return self._href if name == "href" else None


class FakeCard:
    def __init__(self, job_id=None, title="", location="", href=None):
        self.job_id, self.title, self.location, self.href = job_id, title, location, href
        self.clicked = False

    def get_attribute(self, name, timeout=None):
        return self.job_id if name == "data-job-id" else None

    def scroll_into_view_if_needed(self, timeout=None):
        pass

    def locator(self, selector):
        if selector == "a":
            return FakeField(href=self.href, present=self.href is not None)
        return FakeField({"#title": self.title, "#loc": self.location}.get(selector, ""))


class FakeLocator:
    def __init__(self, cards):
        self.cards = cards

    def count(self):
        return len(self.cards)

    def nth(self, index):
        return self.cards[index]


class FakePage:
    def __init__(self, cards, url="https://hiring.amazon.ca/app"):
        self.cards = cards
        self.url = url

    def locator(self, selector):
        return FakeLocator(self.cards)


@pytest.fixture
def wired_selectors(monkeypatch):
    """A simple synthetic card layout, independent of Amazon's real one."""
    monkeypatch.setitem(site_selectors.SELECTORS, "job_card", ".card")
    monkeypatch.setitem(site_selectors.SELECTORS, "card_title", "#title")
    monkeypatch.setitem(site_selectors.SELECTORS, "card_location", "#loc")
    monkeypatch.setitem(site_selectors.SELECTORS, "card_schedule", "#sched")
    monkeypatch.setitem(site_selectors.SELECTORS, "card_pay", "#pay")
    # Amazon's real cards expose neither, but the code paths still exist for
    # sites/layouts that do.
    monkeypatch.setitem(site_selectors.SELECTORS, "card_id_attr", "data-job-id")
    monkeypatch.setitem(site_selectors.SELECTORS, "card_link", "a")


def test_find_matching_card_picks_the_right_card_by_id(wired_selectors):
    """Regression: this used to return the first card on the page regardless
    of which shift matched — i.e. it would hold the wrong shift."""
    cards = [
        FakeCard("JOB-1", "Sorter", "Brampton"),
        FakeCard("JOB-2", "Picker", "Ottawa"),
        FakeCard("JOB-3", "Stower", "Calgary"),
    ]
    page = FakePage(cards)
    found = site_selectors.find_matching_card(page, Shift(id="JOB-3", title="Stower"))
    assert found is cards[2]


def test_find_matching_card_falls_back_to_title_and_location(wired_selectors):
    cards = [FakeCard(None, "Sorter", "Brampton"), FakeCard(None, "Picker", "Ottawa")]
    page = FakePage(cards)
    found = site_selectors.find_matching_card(page, Shift(title="Picker", location="Ottawa"))
    assert found is cards[1]


def test_find_matching_card_returns_none_when_absent(wired_selectors):
    page = FakePage([FakeCard("JOB-1", "Sorter", "Brampton")])
    assert site_selectors.find_matching_card(page, Shift(id="JOB-9", title="Ghost")) is None


def test_find_matching_card_does_not_confuse_same_title_different_location(wired_selectors):
    cards = [FakeCard(None, "Sorter", "Brampton"), FakeCard(None, "Sorter", "Ottawa")]
    page = FakePage(cards)
    found = site_selectors.find_matching_card(page, Shift(title="Sorter", location="Ottawa"))
    assert found is cards[1]


def test_extract_shifts_makes_relative_hrefs_absolute(wired_selectors):
    """page.goto() needs an absolute URL; cards carry hrefs like '/job/123'."""
    page = FakePage(
        [FakeCard("JOB-1", "Sorter", "Brampton", href="/app#/jobDetail?jobId=JOB-1")],
        url="https://hiring.amazon.ca/app#/jobSearch",
    )
    shifts = site_selectors.extract_shifts(page)
    assert shifts[0].url == "https://hiring.amazon.ca/app#/jobDetail?jobId=JOB-1"


def test_extract_shifts_reads_all_fields(wired_selectors):
    page = FakePage([FakeCard("JOB-1", "Sorter", "Brampton")])
    shifts = site_selectors.extract_shifts(page)
    assert len(shifts) == 1
    assert shifts[0].id == "JOB-1"
    assert shifts[0].title == "Sorter"
    assert shifts[0].location == "Brampton"
    assert shifts[0].url is None  # no anchor on the card


def test_extract_shifts_refuses_to_run_on_placeholder_selectors(monkeypatch):
    monkeypatch.setitem(site_selectors.SELECTORS, "job_card", site_selectors.TODO)
    with pytest.raises(RuntimeError, match="job_card"):
        site_selectors.extract_shifts(FakePage([]))


def test_hold_shift_refuses_when_selectors_are_placeholders(monkeypatch):
    """Load-bearing: a stale selector must stop the click path, not have it
    guess at buttons on a real application form."""
    monkeypatch.setattr(site_selectors, "CREATE_APPLICATION", site_selectors.TODO)
    result = site_selectors.hold_shift(FakePage([]), Shift(title="x"))
    assert not result.held
    assert "not configured" in result.message


# ── hot mode ────────────────────────────────────────────────────────────────
def test_hot_windows_parse_to_minute_offsets():
    assert config_mod.parse_hot_windows(["06:00-09:30"]) == [(360, 570)]
    assert config_mod.parse_hot_windows([]) == []


@pytest.mark.parametrize("bad", ["breakfast", "6-9", "25:00-26:00", "06:00-06:00", "06:00"])
def test_malformed_hot_window_fails_at_startup(bad):
    """A window that silently never opens looks exactly like a quiet day, so
    anything unparseable has to be fatal at load time."""
    with pytest.raises(ValueError):
        config_mod.parse_hot_windows([bad])


def test_hot_window_membership():
    windows = config_mod.parse_hot_windows(["06:00-09:00"])
    assert config_mod.in_hot_window(datetime(2026, 8, 17, 6, 0), windows)
    assert config_mod.in_hot_window(datetime(2026, 8, 17, 8, 59), windows)
    assert not config_mod.in_hot_window(datetime(2026, 8, 17, 9, 0), windows)  # end exclusive
    assert not config_mod.in_hot_window(datetime(2026, 8, 17, 5, 59), windows)


def test_hot_window_can_wrap_midnight():
    windows = config_mod.parse_hot_windows(["22:00-02:00"])
    for hour in (22, 23, 0, 1):
        assert config_mod.in_hot_window(datetime(2026, 8, 17, hour, 30), windows), hour
    assert not config_mod.in_hot_window(datetime(2026, 8, 17, 3, 0), windows)


def test_hot_interval_floor_is_enforced():
    cfg = config_mod._deep_merge(config_mod.DEFAULTS, {"polling": {"hot_interval_seconds": 1}})
    with pytest.raises(ValueError, match="hot_interval_seconds"):
        config_mod.validate_config(cfg)


def test_hot_interval_may_not_exceed_the_idle_interval():
    """Hot mode is the fast cadence. Inverting them would silently slow the
    watcher down exactly when a batch is landing."""
    cfg = config_mod._deep_merge(
        config_mod.DEFAULTS,
        {"polling": {"interval_seconds": 20, "hot_interval_seconds": 45}},
    )
    with pytest.raises(ValueError, match="hot_interval_seconds"):
        config_mod.validate_config(cfg)


def test_validate_parses_windows_so_a_typo_never_reaches_the_loop():
    cfg = config_mod._deep_merge(config_mod.DEFAULTS, {"polling": {"hot_windows": ["nope"]}})
    with pytest.raises(ValueError):
        config_mod.validate_config(cfg)


# ── detection log ───────────────────────────────────────────────────────────
def test_detections_are_logged_and_read_back(tmp_path):
    store = StateStore(
        tmp_path / "seen.json", 72, detections_path=tmp_path / "detections.jsonl"
    )
    store.log_detection("id-1", "Sorter — Brampton")
    store.log_detection("id-2", "Picker — Mississauga")
    entries = store.read_detections()
    assert [e["id"] for e in entries] == ["id-1", "id-2"]
    assert all(isinstance(e["ts"], float) for e in entries)


def test_torn_detection_lines_are_skipped_not_fatal(tmp_path):
    """The process can be killed mid-append at any moment; a half-written last
    line must not take the report down with it."""
    path = tmp_path / "detections.jsonl"
    path.write_text('{"ts": 1, "id": "ok"}\n{"ts": 2, "id": "tor', encoding="utf-8")
    store = StateStore(tmp_path / "seen.json", 72, detections_path=path)
    assert [e["id"] for e in store.read_detections()] == ["ok"]


def test_detection_logging_is_best_effort(tmp_path):
    """Analytics must never be able to break detection."""
    store = StateStore(tmp_path / "seen.json", 72, detections_path=None)
    store.log_detection("id-1", "no path configured")  # must not raise
    assert store.read_detections() == []


# ── drop report ─────────────────────────────────────────────────────────────
def _at(hour: int, day: int = 17) -> dict:
    return {"ts": datetime(2026, 8, day, hour, 30).timestamp()}


def test_hourly_counts_uses_local_time():
    counts = drop_report.hourly_counts([_at(6), _at(6), _at(9)])
    assert counts == {6: 2, 9: 1}


def test_adjacent_busy_hours_merge_into_one_window():
    entries = [_at(6)] * 5 + [_at(7)] * 4 + [_at(9)] * 3
    assert drop_report.suggest_windows(drop_report.hourly_counts(entries)) == [
        "06:00-08:00",
        "09:00-10:00",
    ]


def test_a_single_stray_detection_does_not_earn_a_window():
    entries = [_at(6)] * 10 + [_at(3)]
    assert drop_report.suggest_windows(drop_report.hourly_counts(entries)) == ["06:00-07:00"]


def test_suggested_windows_always_parse():
    """Whatever the report prints has to load — including the 23:00 wrap."""
    entries = [_at(23)] * 5 + [_at(0)] * 5
    windows = drop_report.suggest_windows(drop_report.hourly_counts(entries))
    assert windows
    config_mod.parse_hot_windows(windows)


def test_report_with_no_data_explains_itself_instead_of_crashing():
    text = drop_report.render([])
    assert "No detections logged yet" in text


def test_report_renders_a_histogram_and_a_suggestion():
    text = drop_report.render([_at(6)] * 3 + [_at(9)])
    assert "06:00" in text
    assert "hot_windows" in text


# ── the watcher's own scheduling ────────────────────────────────────────────
def _watcher(tmp_path, **polling):
    import watcher as watcher_mod

    cfg = config_mod._deep_merge(
        config_mod.DEFAULTS,
        {
            "polling": polling,
            "state": {
                "path": str(tmp_path / "seen.json"),
                "detections_path": str(tmp_path / "detections.jsonl"),
            },
            "notifications": {"telegram": {"enabled": False}},
        },
    )
    config_mod.validate_config(cfg)
    return watcher_mod.Watcher(cfg)


def test_a_match_turns_hot_mode_on_then_it_expires(tmp_path):
    """The batching bet: one shift appearing means the next is probably seconds
    away, so the cadence tightens for a while afterwards."""
    w = _watcher(tmp_path, hot_duration_seconds=120)
    assert not w.is_hot()
    w.go_hot()
    assert w.is_hot()

    w.hot_until = time.time() - 1  # let it lapse
    assert not w.is_hot()


def test_hot_mode_uses_the_fast_cadence(tmp_path):
    w = _watcher(tmp_path, interval_seconds=45, jitter_seconds=20, hot_interval_seconds=20)
    idle, was_hot = w._next_delay()
    assert not was_hot and 45 <= idle <= 65

    w.go_hot()
    hot, was_hot = w._next_delay()
    assert was_hot
    # Fast, but never a metronome — some jitter survives.
    assert 20 <= hot <= 26
    assert hot < idle


def test_a_clock_window_makes_it_hot_without_any_match(tmp_path):
    w = _watcher(tmp_path, hot_windows=["06:00-09:00"])
    assert w.is_hot(datetime(2026, 8, 17, 6, 30))
    assert not w.is_hot(datetime(2026, 8, 17, 12, 0))


def test_go_hot_never_shortens_an_existing_hot_period(tmp_path):
    w = _watcher(tmp_path, hot_duration_seconds=120)
    w.hot_until = time.time() + 600
    w.go_hot()
    assert w.hot_until > time.time() + 500


# ── api auth: the token that rotates ────────────────────────────────────────
class FakeResponse:
    def __init__(self, status=200, payload=None):
        self.status = status
        self.ok = 200 <= status < 300
        self._payload = payload if payload is not None else {"cards": []}

    def json(self):
        return self._payload

    def text(self):
        return "body"


class FakeRequestContext:
    """Records the headers of every call and replays a scripted response."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, data=None, headers=None, timeout=None):
        self.calls.append({"url": url, "data": data, "headers": headers or {}})
        return self.responses.pop(0) if self.responses else FakeResponse()

    get = post


API_CFG = {
    "endpoint_url": "https://hiring.amazon.ca/graphql",
    "method": "POST",
    "payload": {"q": 1},
    "shifts_path": "cards",
    "field_map": {"id": "jobId", "title": "jobTitle"},
}


def test_token_provider_supplies_the_auth_header():
    ctx = FakeRequestContext([FakeResponse(200, {"cards": [{"jobId": "1", "jobTitle": "A"}]})])
    client = api_client.ApiClient(ctx, API_CFG, token_provider=lambda: "tok-abc")
    assert client.fetch_shifts()[0].id == "1"
    assert ctx.calls[0]["headers"]["authorization"] == "tok-abc"


def test_a_401_refreshes_the_token_and_retries_once():
    """The token rotates. Without this, expiry looks exactly like 'no shifts
    today' — a silent outage, which is the worst failure this tool can have."""
    tokens = iter(["stale", "fresh"])
    current = {"tok": next(tokens)}
    refreshed = []

    def refresh():
        current["tok"] = next(tokens)
        refreshed.append(True)

    ctx = FakeRequestContext([
        FakeResponse(401),
        FakeResponse(200, {"cards": [{"jobId": "9", "jobTitle": "Sorter"}]}),
    ])
    client = api_client.ApiClient(
        ctx, API_CFG, token_provider=lambda: current["tok"], on_unauthorized=refresh
    )

    shifts = client.fetch_shifts()
    assert [s.id for s in shifts] == ["9"]
    assert refreshed, "a 401 must trigger a token refresh"
    assert ctx.calls[0]["headers"]["authorization"] == "stale"
    assert ctx.calls[1]["headers"]["authorization"] == "fresh"


def test_a_second_401_gives_up_instead_of_looping():
    ctx = FakeRequestContext([FakeResponse(401), FakeResponse(401)])
    client = api_client.ApiClient(
        ctx, API_CFG, token_provider=lambda: "t", on_unauthorized=lambda: None
    )
    with pytest.raises(api_client.Unauthorized):
        client.fetch_shifts()
    assert len(ctx.calls) == 2  # one retry, not a retry storm


def test_a_500_is_not_treated_as_an_auth_problem():
    ctx = FakeRequestContext([FakeResponse(500)])
    refreshed = []
    client = api_client.ApiClient(
        ctx, API_CFG, token_provider=lambda: "t",
        on_unauthorized=lambda: refreshed.append(True),
    )
    with pytest.raises(RuntimeError) as excinfo:
        client.fetch_shifts()
    assert not isinstance(excinfo.value, api_client.Unauthorized)
    assert not refreshed


class FakeTokenPage:
    """Just enough page to exercise TokenSource without a browser."""

    def __init__(self, storage=None):
        self.handlers = {}
        self.storage = storage or {}
        self.reloads = 0
        self.gotos = []

    def on(self, event, handler):
        self.handlers[event] = handler

    def fire_request(self, url, headers):
        self.handlers["request"](FakeRequest(url, headers))

    def evaluate(self, _script, key=None):
        return self.storage.get(key)

    def reload(self, **_kwargs):
        self.reloads += 1

    def goto(self, url, **_kwargs):
        self.gotos.append(url)

    def wait_for_timeout(self, _ms):
        pass


class FakeRequest:
    def __init__(self, url, headers):
        self.url = url
        self._headers = headers

    def all_headers(self):
        return self._headers


def test_token_is_harvested_off_the_page_s_own_requests():
    """Whatever the site's JS does to mint a token, it has to put the result on
    the wire — so watching requests needs no knowledge of its internals."""
    page = FakeTokenPage()
    source = auth_token.TokenSource(page, endpoint_url="https://hiring.amazon.ca/graphql")
    assert source.current() is None

    page.fire_request("https://hiring.amazon.ca/graphql", {"authorization": "tok-1"})
    assert source.current() == "tok-1"

    page.fire_request("https://hiring.amazon.ca/graphql", {"authorization": "tok-2"})
    assert source.current() == "tok-2", "the newest token wins"


def test_unrelated_requests_do_not_clobber_the_token():
    page = FakeTokenPage()
    source = auth_token.TokenSource(page, endpoint_url="https://hiring.amazon.ca/graphql")
    page.fire_request("https://hiring.amazon.ca/graphql", {"authorization": "tok-1"})
    page.fire_request("https://telemetry.example.com/x", {"authorization": "somebody-elses"})
    assert source.current() == "tok-1"


def test_local_storage_wins_when_it_has_a_value():
    """The page updates storage on rotation; our captured header is only as new
    as the last request the page happened to make."""
    page = FakeTokenPage(storage={"sessionToken": "from-storage"})
    source = auth_token.TokenSource(
        page, endpoint_url="https://hiring.amazon.ca/graphql", storage_key="sessionToken"
    )
    page.fire_request("https://hiring.amazon.ca/graphql", {"authorization": "older"})
    assert source.current() == "from-storage"


def test_refresh_reloads_the_page_that_mints_tokens():
    page = FakeTokenPage(storage={"sessionToken": "t"})
    source = auth_token.TokenSource(
        page,
        endpoint_url="https://hiring.amazon.ca/graphql",
        storage_key="sessionToken",
        reload_url="https://hiring.amazon.ca/app#/jobSearch",
    )
    assert source.refresh() == "t"
    assert page.gotos == ["https://hiring.amazon.ca/app#/jobSearch"]


def test_a_broken_request_event_cannot_disturb_polling():
    """This handler runs on Playwright's event thread; an exception there must
    not be able to take down the watcher."""
    page = FakeTokenPage()
    source = auth_token.TokenSource(page, endpoint_url="https://hiring.amazon.ca/graphql")

    class Exploding:
        url = "https://hiring.amazon.ca/graphql"

        def all_headers(self):
            raise RuntimeError("boom")

    page.handlers["request"](Exploding())  # must not raise
    assert source.current() is None


# ── batch safety: a hundred jobs can land in one poll ───────────────────────
class RecordingNotifier:
    def __init__(self):
        self.shifts, self.texts = [], []

    def notify_shift(self, shift, dry_run=True):
        self.shifts.append(shift)
        return True

    def send_text(self, text):
        self.texts.append(text)
        return True

    def notify_error(self, message):
        self.texts.append(message)
        return True

    def describe(self, shift):
        # Same shape as the real one, so a message built from it can be
        # asserted on without pulling in HTML escaping.
        return shift.summary()

    def notify_held(self, shift, stopped_before_submit=True):
        return True

    def send_photo(self, path, caption=""):
        return True


def _batch_watcher(tmp_path, shifts, **overrides):
    import watcher as watcher_mod

    cfg = config_mod._deep_merge(
        config_mod.DEFAULTS,
        {
            "state": {
                "path": str(tmp_path / "seen.json"),
                "detections_path": str(tmp_path / "d.jsonl"),
            },
            **overrides,
        },
    )
    config_mod.validate_config(cfg)
    w = watcher_mod.Watcher(cfg)
    w.notifier = RecordingNotifier()
    w._fetch_shifts = lambda: shifts
    return w


def _many(count):
    return [
        Shift(id=f"JOB-{i}", title=f"Warehouse {i}", location="Brampton", pay_rate=15 + i)
        for i in range(count)
    ]


def test_a_big_batch_is_capped_to_one_digest(tmp_path):
    """Telegram rate-limits a single chat. 99 separate pings would arrive
    slowly, out of order, and bury the shift that mattered."""
    w = _batch_watcher(tmp_path, _many(30), notifications={"max_alerts_per_poll": 8})
    w.poll_once()

    assert len(w.notifier.shifts) == 8
    assert len(w.notifier.texts) == 1
    assert "22 more" in w.notifier.texts[0]
    assert w.alerts == 30, "every match still counts and is logged"


def test_the_best_paying_matches_are_the_ones_you_hear_about(tmp_path):
    w = _batch_watcher(tmp_path, _many(20), notifications={"max_alerts_per_poll": 3})
    w.poll_once()
    alerted = [s.pay_rate for s in w.notifier.shifts]
    assert alerted == sorted(alerted, reverse=True)
    assert alerted[0] == 34  # the top payer of the batch, not the first seen


def test_unknown_pay_sorts_last_rather_than_first(tmp_path):
    shifts = [Shift(id="A", title="No pay listed"), Shift(id="B", title="Pays", pay_rate=19)]
    w = _batch_watcher(tmp_path, shifts, notifications={"max_alerts_per_poll": 1})
    w.poll_once()
    assert w.notifier.shifts[0].id == "B"


def test_only_one_shift_is_held_per_poll(tmp_path):
    """You need to win ONE shift. Holding a whole batch would race itself and
    multiply the clicks that get an account flagged."""
    held = []
    w = _batch_watcher(tmp_path, _many(10), dry_run=False)
    w._hold = lambda shift, **kw: held.append(shift)
    w.poll_once()
    assert len(held) == 1
    assert held[0].pay_rate == 24  # the best of the batch


def test_hold_cap_is_configurable(tmp_path):
    held = []
    w = _batch_watcher(tmp_path, _many(10), dry_run=False, hold={"max_per_poll": 3})
    w._hold = lambda shift, **kw: held.append(shift)
    w.poll_once()
    assert len(held) == 3


def test_a_dry_run_batch_never_holds_anything(tmp_path):
    held = []
    w = _batch_watcher(tmp_path, _many(5))
    w._hold = lambda shift, **kw: held.append(shift)
    w.poll_once()
    assert held == []


def test_every_match_is_deduped_even_the_ones_only_summarised(tmp_path):
    """The summarised tail is still marked seen — otherwise it would re-alert
    on every single poll forever."""
    shifts = _many(20)
    w = _batch_watcher(tmp_path, shifts, notifications={"max_alerts_per_poll": 5})
    w.poll_once()
    w.notifier.shifts.clear()
    w.notifier.texts.clear()
    w.poll_once()
    assert w.notifier.shifts == []
    assert w.notifier.texts == []


# ── priority: which shift do you want MOST ──────────────────────────────────
def _shipped():
    return config_mod.load_config(Path(__file__).resolve().parent.parent / "config.yaml")


def _gta(title, city, pay=23.0):
    return Shift(id=f"{title}|{city}", title=title, location=f"{city}, ON", pay_rate=pay)


FULFILLMENT = "Fulfillment Center Warehouse Associate"
DELIVERY = "Delivery Station Warehouse Associate"
SORT = "Sortation Center Warehouse Associate"


def test_closer_city_beats_better_role():
    """The settled rule: location first. A Delivery job in Brampton outranks a
    Fulfillment job in Toronto, because the commute matters more."""
    ranker = ShiftRanker(_shipped()["priority"])
    ranked = ranker.sort([_gta(FULFILLMENT, "Toronto"), _gta(DELIVERY, "Brampton")])
    assert ranked[0].location.startswith("Brampton")


def test_within_one_city_fulfillment_beats_delivery():
    ranker = ShiftRanker(_shipped()["priority"])
    ranked = ranker.sort([_gta(DELIVERY, "Brampton"), _gta(FULFILLMENT, "Brampton")])
    assert ranked[0].title == FULFILLMENT


def test_the_configured_city_order_is_respected():
    ranker = ShiftRanker(_shipped()["priority"])
    ranked = ranker.sort([
        _gta(FULFILLMENT, "Toronto"),
        _gta(FULFILLMENT, "Mississauga"),
        _gta(FULFILLMENT, "Brampton"),
    ])
    assert [s.location.split(",")[0] for s in ranked] == ["Brampton", "Mississauga", "Toronto"]


def test_an_unlisted_but_acceptable_city_ranks_after_the_listed_ones():
    """Milton is inside the 30km net, so it is wanted — just not preferred
    over the three named cities."""
    ranker = ShiftRanker(_shipped()["priority"])
    ranked = ranker.sort([_gta(FULFILLMENT, "Milton"), _gta(FULFILLMENT, "Toronto")])
    assert ranked[0].location.startswith("Toronto")


def test_within_one_city_the_preferred_title_leads_but_nothing_is_demoted():
    """Delivery Station used to sink to the bottom on the assumption it needed
    a driving licence. It does not, so it now competes normally: the title
    preferences still put Fulfillment first, and the rest fall in behind on
    pay rather than being pushed to last."""
    ranker = ShiftRanker(_shipped()["priority"])
    ranked = ranker.sort([
        _gta(DELIVERY, "Brampton", pay=24.0),
        _gta(SORT, "Brampton", pay=22.0),
        _gta(FULFILLMENT, "Brampton", pay=23.0),
    ])
    assert ranked[0].title == FULFILLMENT, "an explicitly preferred title still leads"
    assert ranked[1].title == DELIVERY, "then the better-paid of the rest"
    assert ranked[2].title == SORT


def test_pay_only_breaks_a_tie_nothing_more():
    ranker = ShiftRanker(_shipped()["priority"])
    ranked = ranker.sort([
        _gta(FULFILLMENT, "Brampton", pay=21.0),
        _gta(FULFILLMENT, "Brampton", pay=25.0),
    ])
    assert ranked[0].pay_rate == 25.0
    # ...but never outranks a closer city.
    ranked = ranker.sort([_gta(FULFILLMENT, "Toronto", 99.0), _gta(FULFILLMENT, "Brampton", 20.0)])
    assert ranked[0].location.startswith("Brampton")


def test_the_shipped_filters_accept_what_they_should():
    matcher = ShiftMatcher(_shipped()["filters"])
    for title in (FULFILLMENT, DELIVERY, SORT):
        for city in ("Brampton", "Mississauga", "Toronto", "Oakville", "Milton", "Etobicoke"):
            assert matcher.matches(_gta(title, city))[0], (title, city)


def test_the_shipped_filters_reject_what_they_should():
    matcher = ShiftMatcher(_shipped()["filters"])
    assert not matcher.matches(_gta(FULFILLMENT, "Ottawa"))[0]
    assert not matcher.matches(_gta(FULFILLMENT, "Vancouver"))[0]
    assert not matcher.matches(Shift(title="Delivery Driver", location="Brampton, ON"))[0]


def test_substring_matching_cannot_send_you_across_the_country():
    """'maple' is in the include list for Maple, ON — and would also match
    Maple Ridge, BC without the province excludes."""
    matcher = ShiftMatcher(_shipped()["filters"])
    assert matcher.matches(_gta(FULFILLMENT, "Maple"))[0]
    assert not matcher.matches(Shift(title=FULFILLMENT, location="Maple Ridge, BC"))[0]


def test_no_pay_filter_is_configured():
    """These all pay about the same, so a threshold could only ever drop a
    shift that was wanted."""
    assert _shipped()["filters"]["min_pay_rate"] is None
    matcher = ShiftMatcher(_shipped()["filters"])
    assert matcher.matches(Shift(title=FULFILLMENT, location="Brampton, ON"))[0]


def test_the_watcher_holds_the_top_ranked_shift_of_a_batch(tmp_path):
    """The end of the whole chain: filter, rank, cap, hold exactly one."""
    shipped = _shipped()
    held = []
    w = _batch_watcher(
        tmp_path,
        [
            _gta(DELIVERY, "Oakville"),
            _gta(FULFILLMENT, "Toronto"),
            _gta(DELIVERY, "Brampton"),
            _gta(FULFILLMENT, "Mississauga"),
        ],
        dry_run=False,
        filters=shipped["filters"],
        priority=shipped["priority"],
    )
    w._hold = lambda shift, **kw: held.append(shift)
    w.poll_once()
    assert len(held) == 1
    assert held[0].location.startswith("Brampton")


# ── the apply flow opens a new tab, and may hit a login wall ────────────────
class FakeLocatorTarget:
    def __init__(self, page, on_click=None):
        self.page = page
        self.on_click = on_click

    @property
    def first(self):
        return self

    def wait_for(self, **_kwargs):
        pass

    def click(self, **_kwargs):
        if self.on_click:
            self.on_click()


class FakeTabContext:
    def __init__(self):
        self.pages = []


class FakeFlowPage:
    """A page that can spawn a popup into its context when clicked."""

    def __init__(self, context, url="https://hiring.amazon.com/app#/jobDetail?jobId=J1",
                 spawns=None):
        self.context = context
        self.url = url
        self.spawns = spawns
        context.pages.append(self)
        self.clicks = []

    def locator(self, selector):
        self.clicks.append(selector)

        def on_click():
            if self.spawns is not None:
                self.spawns()
                self.spawns = None

        return FakeLocatorTarget(self, on_click)

    def wait_for_timeout(self, _ms):
        pass

    def wait_for_load_state(self, *_a, **_kw):
        pass

    def screenshot(self, **_kwargs):
        pass

    def keyboard_press(self, _key):
        pass


def test_a_login_tab_is_reported_as_a_login_problem(monkeypatch):
    """Confirmed live: clicking Apply opened auth.hiring.amazon.com/#/login.
    Job search is public, so detection keeps working while signed out and
    nothing warns you until a hold is attempted."""
    monkeypatch.setattr(site_selectors, "CREATE_APPLICATION", "#create-app")
    # on_detail_page drops the leading "open job" step, so include it.
    monkeypatch.setattr(site_selectors, "HOLD_STEPS", [site_selectors.HoldStep("open job", ":scope"),
        site_selectors.HoldStep("select schedule", "#sched", opens_popup=True),
    ])

    ctx = FakeTabContext()
    page = FakeFlowPage(ctx)
    page.spawns = lambda: FakeFlowPage(ctx, url="https://auth.hiring.amazon.com/#/login")
    monkeypatch.setattr(site_selectors, "on_detail_page", lambda _p: True)
    monkeypatch.setattr(site_selectors, "dismiss_overlays", lambda *a, **k: [])

    result = site_selectors.hold_shift(page, Shift(title="x"))
    assert not result.held
    assert "login" in result.message.lower()
    assert "save_session.py" in result.message


def test_the_flow_follows_the_tab_that_apply_opens(monkeypatch):
    """Without following it, later steps would hunt for buttons on the page we
    already left and fail with a misleading 'button not found'."""
    monkeypatch.setattr(site_selectors, "CREATE_APPLICATION", "#create-app")
    monkeypatch.setattr(site_selectors, "HOLD_STEPS", [site_selectors.HoldStep("open job", ":scope"),
        site_selectors.HoldStep("select schedule", "#sched", opens_popup=True),
        site_selectors.HoldStep("create application", "#create"),
    ])
    monkeypatch.setattr(site_selectors, "on_detail_page", lambda _p: True)
    monkeypatch.setattr(site_selectors, "dismiss_overlays", lambda *a, **k: [])

    ctx = FakeTabContext()
    first = FakeFlowPage(ctx)
    application = []

    def spawn():
        application.append(
            FakeFlowPage(ctx, url="https://hiring.amazon.com/application/#/consent")
        )

    first.spawns = spawn

    result = site_selectors.hold_shift(first, Shift(title="x"))
    assert result.status in (site_selectors.CONFIRMED, site_selectors.UNCERTAIN), result.message
    # The second step ran on the NEW tab, not the original page.
    assert application and "#create" in application[0].clicks
    assert "#create" not in first.clicks


def test_is_login_page_recognises_the_portal_and_ignores_job_pages():
    class P:
        def __init__(self, url):
            self.url = url

    assert site_selectors.is_login_page(P("https://auth.hiring.amazon.com/#/login"))
    assert site_selectors.is_login_page(P("https://www.amazon.com/ap/signin?x=1"))
    assert not site_selectors.is_login_page(
        P("https://hiring.amazon.ca/app#/jobDetail?jobId=JOB-CA-1")
    )


def test_pick_a_shift_is_scoped_to_the_schedule_flyout(monkeypatch):
    """An unscoped Apply selector could match a button elsewhere on the page."""
    monkeypatch.setattr(site_selectors, "CREATE_APPLICATION", "#create-app")
    # The real steps, minus the one still behind the login wall.
    monkeypatch.setattr(site_selectors, "HOLD_STEPS", [site_selectors.HoldStep("open job", ":scope"),
        site_selectors.HoldStep("select schedule", "[data-test-id='jobDetailSelectScheduleButton']"),
        site_selectors.HoldStep("pick a shift", "[data-test-id='ScheduleCardSelectScheduleLink']", opens_popup=True),
    ])
    monkeypatch.setattr(site_selectors, "on_detail_page", lambda _p: True)
    monkeypatch.setattr(site_selectors, "dismiss_overlays", lambda *a, **k: [])

    ctx = FakeTabContext()
    page = FakeFlowPage(ctx)
    site_selectors.hold_shift(page, Shift(title="x"))
    picked = [c for c in page.clicks if "ScheduleCardSelectScheduleLink" in c]
    assert picked, page.clicks
    assert picked[0].startswith(site_selectors.SCHEDULE_FLYOUT)


def test_the_screenshot_waits_for_the_application_to_render(monkeypatch, tmp_path):
    """Regression: the tab Apply opens is blank for a second or two, so
    capturing immediately produced a plain white image. A Telegram alert
    carrying a blank photo reads as a failure even when the hold worked."""
    events = []

    class Target:
        @property
        def first(self):
            return self

        def wait_for(self, **_kwargs):
            events.append("waited-for-application")

        def click(self, **_kwargs):
            pass

    class Page:
        url = "https://hiring.amazon.com/application/us/?scheduleId=SCH-1"
        context = None

        def locator(self, _selector):
            return Target()

        def screenshot(self, **_kwargs):
            events.append("screenshot")

        def wait_for_timeout(self, _ms):
            pass

    monkeypatch.setattr(site_selectors, "HOLD_STEPS", [site_selectors.HoldStep("open job", ":scope")])
    monkeypatch.setattr(site_selectors, "on_detail_page", lambda _p: True)
    monkeypatch.setattr(site_selectors, "dismiss_overlays", lambda *a, **k: [])

    result = site_selectors.hold_shift(
        Page(), Shift(title="x"), screenshot_path=str(tmp_path / "shot.png")
    )
    assert result.status != site_selectors.FAILED, result.message
    assert events == ["waited-for-application", "screenshot"], events
    assert "scheduleId=SCH-1" in result.message, "the alert must carry the link to finish"


# ── the one click that actually holds the spot ──────────────────────────────
class ConsentPage:
    """The consent screen, and what it becomes once the application exists."""

    def __init__(self, banner_after_click=True, has_button=True):
        self.url = "https://hiring.amazon.com/application/us/?scheduleId=SCH-9#/consent"
        self.context = None
        self.clicked = []
        self.banner = ""
        self.banner_after_click = banner_after_click
        self.has_button = has_button
        self.shots = 0

    def locator(self, selector):
        page = self

        class L:
            @property
            def first(self):
                return self

            def wait_for(self, **_kw):
                if "Create Application" in selector and not page.has_button:
                    raise RuntimeError("not visible")

            def click(self, **_kw):
                page.clicked.append(selector)
                if "Create Application" in selector and page.banner_after_click:
                    page.banner = (
                        "We are holding a spot for you for the next 2 hours "
                        "and 59 minutes to complete the remaining steps."
                    )

        return L()

    def evaluate(self, _script, _arg=None):
        return self.banner

    def wait_for_timeout(self, _ms):
        pass

    def screenshot(self, **_kw):
        self.shots += 1


def _consent_flow(monkeypatch):
    monkeypatch.setattr(site_selectors, "HOLD_STEPS", [site_selectors.HoldStep("open job", ":scope")])
    monkeypatch.setattr(site_selectors, "on_detail_page", lambda _p: True)
    monkeypatch.setattr(site_selectors, "dismiss_overlays", lambda *a, **k: [])


def test_the_default_run_never_presses_create_application(monkeypatch):
    """Everything before it is reversible browsing. This click accepts the age
    and drug-test declarations, so it must not happen by default."""
    _consent_flow(monkeypatch)
    page = ConsentPage()
    result = site_selectors.hold_shift(page, Shift(title="x"), stop_before_submit=True)
    assert result.status == site_selectors.UNCERTAIN, "stopping short is not a hold"
    assert not result.held
    assert page.clicked == [], "nothing may be clicked on the consent screen"
    assert "NOT held" in result.message
    assert "stop_before_submit" in result.message


def test_pressing_create_application_holds_the_spot_and_reads_the_banner(monkeypatch):
    """The site states the hold itself, with a countdown — so report that
    rather than assuming a click that appeared to work did work."""
    _consent_flow(monkeypatch)
    page = ConsentPage()
    result = site_selectors.hold_shift(page, Shift(title="x"), stop_before_submit=False)
    assert result.held
    assert any("Create Application" in c for c in page.clicked)
    assert "SPOT HELD" in result.message
    assert "2 hours and 59 minutes" in result.message
    assert "scheduleId=SCH-9" in result.message


def test_a_missing_banner_is_reported_honestly_rather_than_claimed(monkeypatch):
    _consent_flow(monkeypatch)
    page = ConsentPage(banner_after_click=False)
    result = site_selectors.hold_shift(page, Shift(title="x"), stop_before_submit=False)
    assert result.status == site_selectors.UNCERTAIN
    assert not result.held, "an unconfirmed click must never read as held"
    assert result.needs_you, "an unconfirmed hold has to interrupt somebody"
    assert "never appeared" in result.message
    assert "check it by hand" in result.message


def test_a_missing_create_button_fails_instead_of_claiming_a_hold(monkeypatch):
    _consent_flow(monkeypatch)
    page = ConsentPage(has_button=False)
    result = site_selectors.hold_shift(page, Shift(title="x"), stop_before_submit=False)
    assert not result.held
    assert "Create Application" in result.message


def test_stopping_early_still_says_the_spot_is_not_held(monkeypatch):
    """The dangerous failure is believing a shift is yours when it is not."""
    _consent_flow(monkeypatch)
    page = ConsentPage(has_button=False)
    result = site_selectors.hold_shift(page, Shift(title="x"), stop_before_submit=True)
    assert "NOT HELD" in result.message.upper()


# ── two environments: Canada for real, the US for testing ───────────────────
def _both_configs():
    root = Path(__file__).resolve().parent.parent
    return config_mod.load_config(root / "config.yaml"), config_mod.load_config(
        root / "config.us.yaml"
    )


def test_the_us_config_loads_and_points_at_the_us_site():
    _, us = _both_configs()
    assert "hiring.amazon.com" in us["site"]["job_search_url"]
    assert "hiring.amazon.com" in us["api"]["endpoint_url"]
    assert us["api"]["extra_headers"]["country"] == "United States"
    assert us["api"]["payload"]["variables"]["searchJobRequest"]["country"] == "United States"


def test_extends_inherits_everything_not_overridden():
    """The US config only lists what differs; the rest must come from the
    Canadian one, or the two drift apart silently."""
    ca, us = _both_configs()
    assert us["polling"]["mode"] == ca["polling"]["mode"]
    assert us["polling"]["hot_interval_seconds"] == ca["polling"]["hot_interval_seconds"]
    assert us["api"]["shifts_path"] == ca["api"]["shifts_path"]
    assert us["api"]["auth_from_page"] == ca["api"]["auth_from_page"]
    assert us["browser"]["channel"] == ca["browser"]["channel"]


def test_a_us_test_run_cannot_touch_canadian_state_or_session():
    """The real risk of a second environment: a test run marking a Canadian
    shift as already-seen, overwriting the Canadian login, or feeding US
    posting times into --drop-report and setting hot_windows to the wrong
    hours."""
    ca, us = _both_configs()
    assert us["state"]["path"] != ca["state"]["path"]
    assert us["state"]["detections_path"] != ca["state"]["detections_path"]
    assert us["browser"]["storage_state"] != ca["browser"]["storage_state"]
    assert us["browser"]["user_data_dir"] != ca["browser"]["user_data_dir"]
    assert us["logging"]["path"] != ca["logging"]["path"]


def test_the_testing_config_stays_safe_by_default():
    """The US config exists to exercise the code. A test run must never start
    a real application unless somebody deliberately edits this file."""
    _, us = _both_configs()
    assert us["dry_run"] is True
    assert us["hold"]["stop_before_submit"] is True


def test_extends_can_be_a_chain(tmp_path):
    (tmp_path / "base.yaml").write_text("polling:\n  interval_seconds: 33\n", "utf-8")
    (tmp_path / "middle.yaml").write_text(
        'extends: "base.yaml"\npolling:\n  jitter_seconds: 7\n', "utf-8")
    (tmp_path / "leaf.yaml").write_text('extends: "middle.yaml"\ndry_run: false\n', "utf-8")
    cfg = config_mod.load_config(tmp_path / "leaf.yaml")
    assert cfg["polling"]["interval_seconds"] == 33
    assert cfg["polling"]["jitter_seconds"] == 7
    assert cfg["dry_run"] is False


def test_an_extends_loop_is_caught_instead_of_hanging(tmp_path):
    (tmp_path / "a.yaml").write_text('extends: "b.yaml"\n', "utf-8")
    (tmp_path / "b.yaml").write_text('extends: "a.yaml"\n', "utf-8")
    with pytest.raises(ValueError, match="loop"):
        config_mod.load_config(tmp_path / "a.yaml")


def test_a_missing_parent_config_is_named(tmp_path):
    (tmp_path / "child.yaml").write_text('extends: "nope.yaml"\n', "utf-8")
    with pytest.raises(FileNotFoundError, match="nope.yaml"):
        config_mod.load_config(tmp_path / "child.yaml")


# ── doctor: is this environment actually ready? ─────────────────────────────
def test_verdict_distinguishes_broken_from_merely_unable_to_hold():
    """The distinction the whole command exists for: a signed-out watcher
    still detects and alerts perfectly, so that is a warning, not a failure."""
    ok = [doctor.Check("a", doctor.OK)]
    warn = [doctor.Check("a", doctor.OK), doctor.Check("b", doctor.WARN)]
    fail = [doctor.Check("a", doctor.WARN), doctor.Check("b", doctor.FAIL)]

    assert doctor.verdict(ok)[0] == 0
    assert doctor.verdict(warn)[0] == 1
    assert doctor.verdict(fail)[0] == 2
    assert "cannot hold" in doctor.verdict(warn)[1]
    assert "detection" in doctor.verdict(fail)[1].lower()


class DoctorPage:
    def __init__(self, lands_on):
        self.lands_on = lands_on
        self.url = "about:blank"
        self.visited = []

    def goto(self, url, **_kw):
        self.visited.append(url)
        self.url = self.lands_on

    def wait_for_timeout(self, _ms):
        pass


def test_portal_login_check_needs_no_job_posting():
    """hiring.amazon.ca is empty most of the time, so the check has to work
    with nothing posted. Confirmed live against both states: /application/
    stays put when signed in and bounces to auth.hiring.amazon.com when not."""
    signed_in = DoctorPage("https://hiring.amazon.ca/application/ca/#/pre-consent")
    check = doctor.check_portal_login(signed_in, "https://hiring.amazon.ca", settle_ms=0)
    assert check.state == doctor.OK
    assert signed_in.visited == ["https://hiring.amazon.ca/application/"]

    signed_out = DoctorPage("https://auth.hiring.amazon.com/#/login")
    check = doctor.check_portal_login(signed_out, "https://hiring.amazon.ca", settle_ms=0)
    assert check.state == doctor.WARN
    assert "signed OUT" in check.detail
    assert "save_session.py" in check.fix


def test_a_dead_page_does_not_crash_the_doctor():
    class Broken(DoctorPage):
        def goto(self, url, **_kw):
            raise RuntimeError("net::ERR_CONNECTION_REFUSED")

    check = doctor.check_portal_login(Broken(""), "https://hiring.amazon.ca", settle_ms=0)
    assert check.state == doctor.WARN


def test_zero_jobs_is_reported_not_judged():
    """Canada is empty most of the time. An empty result is healthy."""
    class Client:
        def fetch_shifts(self):
            return []

    class Source:
        def current(self):
            return "tok"

    checks = doctor.check_api(Client(), Source())
    assert all(c.state == doctor.OK for c in checks), [c.render() for c in checks]
    assert "0 job(s)" in checks[-1].detail


def test_an_api_failure_is_a_hard_failure():
    class Client:
        def fetch_shifts(self):
            raise RuntimeError("API returned 500")

    checks = doctor.check_api(Client(), None)
    assert any(c.state == doctor.FAIL for c in checks)


def test_render_lists_the_fixes_once_each():
    checks = [
        doctor.Check("a", doctor.WARN, "x", fix="python save_session.py"),
        doctor.Check("b", doctor.WARN, "y", fix="python save_session.py"),
    ]
    text = doctor.render(checks, "Env")
    assert text.count("python save_session.py") == 1


def test_a_filtered_out_posting_still_leaves_a_trace(tmp_path, caplog):
    """Canadian postings are rare and gone in about a minute. If one is
    filtered out and logged nowhere, you cannot answer the question you will
    definitely ask later: was that one of mine?"""
    shipped = _shipped()
    w = _batch_watcher(
        tmp_path,
        [
            Shift(id="1", title="Fulfillment Center Warehouse Associate",
                  location="Tsawwassen, BC", pay_rate=23.0),
            Shift(id="2", title="Delivery Driver", location="Brampton, ON", pay_rate=23.0),
        ],
        filters=shipped["filters"],
        priority=shipped["priority"],
    )
    with caplog.at_level("INFO"):
        w.poll_once()

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "filtered out" in logged
    assert "Tsawwassen" in logged, "the location is the whole point of the line"
    assert w.notifier.shifts == [], "neither posting should have alerted"


def test_the_doctor_names_the_jobs_it_finds():
    class Client:
        def fetch_shifts(self):
            return [Shift(id="1", title="Fulfillment Center Warehouse Associate",
                          location="Brampton, ON", pay_rate=23.0)]

    class Source:
        def current(self):
            return "tok"

    checks = doctor.check_api(Client(), Source())
    assert "Brampton" in checks[-1].detail


# ── hold-first workflow: the 16 seconds of dead waiting ─────────────────────
def test_only_the_apply_step_declares_a_popup():
    """The measured bug: waiting for a tab after every step cost the full
    timeout twice — 16 of the 28 seconds between spotting a shift and holding
    it. Only Apply opens a tab, and only Apply may pay for one."""
    popup_steps = [s.label for s in site_selectors.HOLD_STEPS if s.opens_popup]
    assert popup_steps == ["pick a shift"]


def test_no_popup_wait_happens_for_a_step_that_opens_nothing(monkeypatch):
    """The regression that matters: a step with opens_popup=False must never
    call the waiter at all, no matter how the loop is refactored."""
    calls = []
    monkeypatch.setattr(site_selectors, "CREATE_APPLICATION", "#create-app")
    monkeypatch.setattr(site_selectors, "dismiss_overlays", lambda *a, **k: [])
    monkeypatch.setattr(site_selectors, "on_detail_page", lambda _p: True)
    monkeypatch.setattr(
        site_selectors, "_wait_for_new_page",
        lambda ctx, known, timeout_ms=0: calls.append(timeout_ms) or None,
    )
    monkeypatch.setattr(site_selectors, "HOLD_STEPS", [
        site_selectors.HoldStep("open job", ":scope"),
        site_selectors.HoldStep("select schedule", "#sched"),
        site_selectors.HoldStep("open the consent screen", "#next"),
    ])

    ctx = FakeTabContext()
    site_selectors.hold_shift(FakeFlowPage(ctx), Shift(title="x"))
    assert calls == [], "no step here opens a tab, so nothing may wait for one"


def test_the_popup_wait_still_happens_for_apply(monkeypatch):
    calls = []
    monkeypatch.setattr(site_selectors, "CREATE_APPLICATION", "#create-app")
    monkeypatch.setattr(site_selectors, "dismiss_overlays", lambda *a, **k: [])
    monkeypatch.setattr(site_selectors, "on_detail_page", lambda _p: True)
    monkeypatch.setattr(
        site_selectors, "_wait_for_new_page",
        lambda ctx, known, timeout_ms=0: calls.append(timeout_ms) or None,
    )
    monkeypatch.setattr(site_selectors, "HOLD_STEPS", [
        site_selectors.HoldStep("open job", ":scope"),
        site_selectors.HoldStep("pick a shift", "#apply", opens_popup=True),
    ])

    ctx = FakeTabContext()
    site_selectors.hold_shift(FakeFlowPage(ctx), Shift(title="x"))
    assert len(calls) == 1, "Apply does open a tab and must still be followed"


def test_a_hold_records_step_timings():
    """You cannot tune what you do not measure, and this is the number the
    whole program exists to shrink."""
    result = site_selectors.HoldResult(
        site_selectors.CONFIRMED, "ok", timings=[("select schedule", 1500.0)],
    )
    assert "select schedule 1500ms" in result.timing_summary()


def test_the_three_hold_states_are_distinct():
    confirmed = site_selectors.HoldResult(site_selectors.CONFIRMED, "held")
    uncertain = site_selectors.HoldResult(site_selectors.UNCERTAIN, "unsure")
    failed = site_selectors.HoldResult(site_selectors.FAILED, "broke")

    assert confirmed.held and not confirmed.needs_you
    assert not uncertain.held and uncertain.needs_you
    assert not failed.held and failed.needs_you


# ── alerts must not sit on the critical path ────────────────────────────────
class SlowNotifier(RecordingNotifier):
    """A notifier that takes a second, like a cold Telegram connection."""

    def __init__(self, order, delay=0.4):
        super().__init__()
        self.order = order
        self.delay = delay

    def notify_shift(self, shift, dry_run=True):
        time.sleep(self.delay)
        self.order.append("alert")
        return super().notify_shift(shift, dry_run=dry_run)


def test_the_hold_starts_before_the_alert_finishes(tmp_path):
    """A Telegram send has been seen to take 10s on a cold connection. The
    shift must not wait on it — the alert goes out in parallel."""
    order = []
    w = _batch_watcher(tmp_path, [Shift(id="1", title="Warehouse", location="Brampton, ON")],
                       dry_run=False)
    w.notifier = SlowNotifier(order)
    w._hold = lambda shift, **kw: order.append("hold")

    w.poll_once()
    assert order and order[0] == "hold", (
        "the hold must begin before a slow alert completes, got %r" % (order,)
    )
    w.drain_notifications(timeout=5)
    assert "alert" in order, "the alert still has to go out"


def test_the_alert_is_dispatched_even_though_the_hold_runs_first(tmp_path):
    """Parallel, not deferred: if the hold hangs or the process dies, you must
    still learn that a shift appeared."""
    w = _batch_watcher(tmp_path, [Shift(id="1", title="Warehouse", location="Brampton, ON")],
                       dry_run=False)
    w._hold = lambda shift, **kw: None
    w.poll_once()
    w.drain_notifications(timeout=5)
    assert w.notifier.shifts, "the matched shift was never alerted"


def test_dry_run_still_alerts_without_clicking(tmp_path):
    w = _batch_watcher(tmp_path, [Shift(id="1", title="Warehouse", location="Brampton, ON")],
                       dry_run=True)
    held = []
    w._hold = lambda shift, **kw: held.append(shift)
    w.poll_once()
    w.drain_notifications(timeout=5)
    assert w.notifier.shifts and not held


def test_hold_disabled_still_alerts_without_clicking(tmp_path):
    w = _batch_watcher(tmp_path, [Shift(id="1", title="Warehouse", location="Brampton, ON")],
                       dry_run=False)
    w.cfg["hold"]["enabled"] = False
    held = []
    w._hold = lambda shift, **kw: held.append(shift)
    w.poll_once()
    w.drain_notifications(timeout=5)
    assert w.notifier.shifts and not held


def test_settling_gives_up_immediately_on_a_login_page():
    """A signed-out session lands on the login page instead of the
    application. Waiting the full 20s for an app that is never coming only
    delays the message telling you to log in."""
    class LoginPage:
        url = "https://auth.hiring.amazon.com/#/login"

        def wait_for_load_state(self, *_a, **_kw):
            pass

        def wait_for_timeout(self, _ms):
            raise AssertionError("must not wait once the login page is visible")

        def locator(self, _selector):
            raise AssertionError("must not probe for buttons on the login page")

    assert site_selectors._settle_after_popup(LoginPage(), timeout_ms=20000) is False


# ── two processes, one browser profile ──────────────────────────────────────
def test_a_locked_profile_is_named_as_such():
    """Chrome allows one process per profile and exits 21 when a second tries.
    Playwright reports that as a TargetClosedError with sixty lines of launch
    flags, which reads like a Playwright bug and sends you debugging the wrong
    thing. It is almost always just the watcher already running."""
    assert browser_launch.looks_like_profile_in_use("<process did exit: exitCode=21>")
    assert browser_launch.looks_like_profile_in_use(
        "BrowserType.launch_persistent_context: Target page, context or browser has been closed"
    )
    assert not browser_launch.looks_like_profile_in_use("net::ERR_CONNECTION_REFUSED")


def test_the_profile_in_use_error_says_how_to_fix_it():
    exc = browser_launch.ProfileInUse(
        "the browser profile browser_profile is already open in another process"
    )
    assert "already open" in str(exc)


# ── session expiry: the silent killer ───────────────────────────────────────
class FakeCheckPage:
    def __init__(self, url):
        self.url = url
        self.closed = False

    def goto(self, url, **_kw):
        pass

    def wait_for_timeout(self, _ms):
        pass

    def close(self):
        self.closed = True


def _session_watcher(tmp_path, signed_in, **overrides):
    overrides.setdefault("dry_run", False)
    w = _batch_watcher(tmp_path, [], **overrides)
    w.session_check_every = 600
    w.next_session_check = 0.0

    url = ("https://hiring.amazon.ca/application/ca/#/pre-consent" if signed_in
           else "https://auth.hiring.amazon.com/#/login")

    class Ctx:
        pages = []

        def new_page(self_inner):
            return FakeCheckPage(url)

    w.context = Ctx()
    return w


def test_an_expired_session_is_detected_and_alerted(tmp_path):
    """Measured live: the CA portal signed itself out after about two hours
    while job search carried on working. Nothing about a poll reveals it, so
    the watcher would alert on a Brampton shift and then fail to hold it."""
    w = _session_watcher(tmp_path, signed_in=False)
    assert w.check_session_if_due() is False
    w.drain_notifications(timeout=5)
    assert any("session expired" in t.lower() for t in w.notifier.texts), w.notifier.texts
    assert any("save_session.py" in t for t in w.notifier.texts)


def test_an_expired_session_is_only_alerted_once(tmp_path):
    """An hourly nag is one you learn to ignore, which is worse than silence."""
    w = _session_watcher(tmp_path, signed_in=False)
    for _ in range(3):
        w.next_session_check = 0.0
        w.check_session_if_due()
    w.drain_notifications(timeout=5)
    assert len(w.notifier.texts) == 1, w.notifier.texts


def test_recovery_is_reported_too(tmp_path):
    w = _session_watcher(tmp_path, signed_in=False)
    w.check_session_if_due()

    healthy = _session_watcher(tmp_path, signed_in=True)
    healthy.notifier = w.notifier
    healthy.session_ok = False
    healthy.next_session_check = 0.0
    assert healthy.check_session_if_due() is True
    healthy.drain_notifications(timeout=5)
    assert any("restored" in t.lower() for t in healthy.notifier.texts)


def test_a_healthy_session_says_nothing(tmp_path):
    w = _session_watcher(tmp_path, signed_in=True)
    assert w.check_session_if_due() is True
    w.drain_notifications(timeout=5)
    assert w.notifier.texts == []


def test_dry_run_does_not_nag_about_the_session(tmp_path):
    """Nothing will be held in dry run, so an expired session costs nothing."""
    w = _session_watcher(tmp_path, signed_in=False, dry_run=True)
    assert w.check_session_if_due() is None
    assert w.notifier.texts == []


def test_the_check_page_is_always_closed(tmp_path):
    """A page leaked every ten minutes would quietly eat the machine."""
    pages = []

    w = _batch_watcher(tmp_path, [], dry_run=False)
    w.session_check_every = 600
    w.next_session_check = 0.0

    class Ctx:
        pages = []

        def new_page(self_inner):
            page = FakeCheckPage("https://auth.hiring.amazon.com/#/login")
            pages.append(page)
            return page

    w.context = Ctx()
    w.check_session_if_due()
    assert pages and all(p.closed for p in pages)


def test_a_broken_check_never_takes_the_loop_down(tmp_path):
    w = _batch_watcher(tmp_path, [], dry_run=False)
    w.session_check_every = 600
    w.next_session_check = 0.0

    class Ctx:
        pages = []

        def new_page(self_inner):
            raise RuntimeError("browser is having a bad day")

    w.context = Ctx()
    assert w.check_session_if_due() is None  # must not raise


def test_the_check_does_not_run_more_often_than_configured(tmp_path):
    w = _session_watcher(tmp_path, signed_in=True)
    assert w.check_session_if_due() is True
    assert w.check_session_if_due() is None, "second call is not due yet"


def test_a_silly_session_check_interval_is_rejected():
    cfg = config_mod._deep_merge(
        config_mod.DEFAULTS, {"session": {"check_every_seconds": 5}}
    )
    with pytest.raises(ValueError, match="check_every_seconds"):
        config_mod.validate_config(cfg)


def test_session_age_is_reported_so_expiry_can_be_measured(tmp_path):
    """Every "it expired after about two hours" so far has been an estimate
    from log timestamps. The next one should be a measurement."""
    import watcher as watcher_mod

    assert watcher_mod.Watcher.format_age(None) == "unknown age"
    assert watcher_mod.Watcher.format_age(7 * 3600 + 5 * 60) == "7h05m old"
    assert watcher_mod.Watcher.format_age(90) == "0h01m old"


def test_the_expiry_alert_says_how_long_it_lasted(tmp_path):
    w = _session_watcher(tmp_path, signed_in=False)
    session_file = tmp_path / "session.json"
    session_file.write_text("{}", encoding="utf-8")
    w.cfg["browser"]["storage_state"] = str(session_file)

    w.check_session_if_due()
    w.drain_notifications(timeout=5)
    assert any("lasted" in t for t in w.notifier.texts), w.notifier.texts


# ── automated re-login (opt-in) ─────────────────────────────────────────────
def test_no_credentials_means_the_feature_is_inert(monkeypatch):
    """Off by default and inert without credentials: the founding property of
    this project is that no code path reads a password unless you opt in."""
    monkeypatch.delenv("AMAZON_LOGIN_EMAIL", raising=False)
    monkeypatch.delenv("AMAZON_LOGIN_PASSWORD", raising=False)
    assert relogin.credentials() is None

    status, detail = relogin.attempt(object(), "https://hiring.amazon.ca")
    assert status == relogin.UNKNOWN
    assert "no credentials" in detail


def test_credentials_come_from_the_environment_not_config(monkeypatch):
    """config.yaml is committed; .env is gitignored."""
    monkeypatch.setenv("AMAZON_LOGIN_EMAIL", "someone@example.com")
    monkeypatch.setenv("AMAZON_LOGIN_PASSWORD", "hunter2")
    assert relogin.credentials() == ("someone@example.com", "hunter2")

    shipped = (Path(__file__).resolve().parent.parent / "config.yaml").read_text("utf-8")
    assert "AMAZON_LOGIN_PASSWORD" not in shipped or "password:" not in shipped.lower()


def test_the_testing_config_never_logs_itself_in():
    """The CA config may enable auto-relogin deliberately. The US test
    environment must not: a test run signing itself into the shared account is
    exactly the interference the two-environment split exists to prevent."""
    _, us = _both_configs()
    assert us["session"]["auto_relogin"] is False


def test_auto_relogin_is_inert_without_credentials(monkeypatch):
    """Turning the flag on is safe on its own — the credentials are a separate,
    deliberate step, and without them nothing is ever typed anywhere."""
    monkeypatch.delenv("AMAZON_LOGIN_EMAIL", raising=False)
    monkeypatch.delenv("AMAZON_LOGIN_PIN", raising=False)
    monkeypatch.delenv("AMAZON_LOGIN_PASSWORD", raising=False)
    assert relogin.credentials() is None


class LoginFake:
    """A login page that can be told what to show at each step."""

    def __init__(self, texts, has_password=True):
        self.texts = list(texts)
        self.has_password = has_password
        self.typed = []
        self.url = "https://auth.hiring.amazon.com/#/login"

    def goto(self, url, **_kw):
        self.url = url

    def wait_for_timeout(self, _ms):
        pass

    def inner_text(self, _sel):
        return self.texts.pop(0) if self.texts else ""

    def locator(self, selector):
        page = self

        class L:
            def __init__(self, sel):
                self.sel = sel

            @property
            def first(self):
                return self

            def count(self):
                return 0 if ("password" in self.sel and not page.has_password) else 1

            def is_visible(self):
                return True

            def wait_for(self, **_kw):
                if "password" in self.sel and not page.has_password:
                    raise RuntimeError("no password field")

            def fill(self, value):
                page.typed.append(value)

            def click(self, **_kw):
                pass

        return L(selector)


def test_an_otp_challenge_stops_instead_of_guessing(monkeypatch):
    """It cannot answer an OTP, and pretending otherwise would burn login
    attempts on an account that then gets locked."""
    monkeypatch.setenv("AMAZON_LOGIN_EMAIL", "someone@example.com")
    monkeypatch.setenv("AMAZON_LOGIN_PASSWORD", "hunter2")
    page = LoginFake(["", "please enter the verification code we sent"])
    status, detail = relogin.attempt(page, "https://hiring.amazon.ca")
    assert status == relogin.OTP_REQUIRED
    assert "verification code" in detail
    assert "hunter2" not in page.typed, "the password must never be typed into a challenge"


def test_rejected_credentials_are_reported_and_not_retried(monkeypatch):
    monkeypatch.setenv("AMAZON_LOGIN_EMAIL", "someone@example.com")
    monkeypatch.setenv("AMAZON_LOGIN_PASSWORD", "wrong")
    page = LoginFake(["", "", "the password you entered is incorrect"])
    status, detail = relogin.attempt(page, "https://hiring.amazon.ca")
    assert status == relogin.BAD_CREDENTIALS
    assert ".env" in detail


def test_a_changed_login_flow_is_reported_rather_than_forced(monkeypatch):
    monkeypatch.setenv("AMAZON_LOGIN_EMAIL", "someone@example.com")
    monkeypatch.setenv("AMAZON_LOGIN_PASSWORD", "hunter2")
    page = LoginFake(["", ""], has_password=False)
    status, detail = relogin.attempt(page, "https://hiring.amazon.ca")
    assert status == relogin.UNKNOWN
    assert "PIN field" in detail


def test_relogin_is_attempted_once_per_expiry_not_once_per_check(tmp_path):
    """Repeated failed logins are how accounts get locked."""
    w = _session_watcher(tmp_path, signed_in=False)
    w.auto_relogin = True
    attempts = []
    w.try_relogin = lambda: attempts.append(1) or False

    for _ in range(4):
        w.next_session_check = 0.0
        w.check_session_if_due()
    assert len(attempts) == 1, "one attempt per expiry"


def test_a_recovered_session_rearms_the_next_attempt(tmp_path):
    w = _session_watcher(tmp_path, signed_in=True)
    w.auto_relogin = True
    w.relogin_tried = True
    w.check_session_if_due()
    assert w.relogin_tried is False


def test_a_successful_relogin_suppresses_the_expiry_alert(tmp_path):
    w = _session_watcher(tmp_path, signed_in=False)
    w.auto_relogin = True
    w.try_relogin = lambda: True
    assert w.check_session_if_due() is True
    w.drain_notifications(timeout=5)
    assert not any("expired" in t.lower() for t in w.notifier.texts), w.notifier.texts


def test_the_credential_is_a_six_digit_pin_not_a_password(monkeypatch):
    """Learned from the competing service's own signup bot: Amazon Hiring
    accounts use a 6-digit PIN. AMAZON_LOGIN_PASSWORD still works as a name."""
    monkeypatch.setenv("AMAZON_LOGIN_EMAIL", "someone@example.com")
    monkeypatch.delenv("AMAZON_LOGIN_PASSWORD", raising=False)
    monkeypatch.setenv("AMAZON_LOGIN_PIN", "485469")
    assert relogin.credentials() == ("someone@example.com", "485469")

    monkeypatch.delenv("AMAZON_LOGIN_PIN")
    monkeypatch.setenv("AMAZON_LOGIN_PASSWORD", "485469")
    assert relogin.credentials() == ("someone@example.com", "485469")


def test_a_pin_that_is_not_six_digits_warns_but_still_tries(monkeypatch, caplog):
    """Amazon may vary it, so this is a warning rather than a refusal — but a
    wrong credential burns one of the very few attempts allowed."""
    monkeypatch.setenv("AMAZON_LOGIN_EMAIL", "someone@example.com")
    monkeypatch.setenv("AMAZON_LOGIN_PIN", "hunter2")
    with caplog.at_level("WARNING"):
        assert relogin.credentials() == ("someone@example.com", "hunter2")
    assert "6-digit PIN" in caplog.text


class SplitPinPage:
    """A PIN screen made of six single-character boxes."""

    def __init__(self):
        self.digits = []

    def locator(self, selector):
        page = self

        class Boxes:
            def count(self_inner):
                return 6 if "maxlength" in selector else 0

            def nth(self_inner, index):
                return Box()

            @property
            def first(self_inner):
                return Box()

        class Box:
            def fill(self_inner, value):
                page.digits.append(value)

            def wait_for(self_inner, **_kw):
                raise RuntimeError("single field does not exist here")

        return Boxes()


def test_a_split_digit_pin_field_gets_every_digit():
    """A plain fill() on the first box would enter one character, which reads
    as a wrong PIN and burns an attempt."""
    page = SplitPinPage()
    assert relogin._enter_pin(page, "485469") is True
    assert page.digits == ["4", "8", "5", "4", "6", "9"]


def test_the_country_is_chosen_from_the_site_being_watched():
    """The login form requires a country, and skipping it fails in a way that
    looks like something else: Continue never becomes clickable, so the first
    real attempt died on a 20s click timeout with nothing to do with
    credentials."""
    assert relogin.country_for("https://hiring.amazon.ca") == "Canada"
    assert relogin.country_for("https://hiring.amazon.com") == "United States"
    assert relogin.country_for("") == "Canada"


def test_an_emailed_verification_code_stops_the_attempt(monkeypatch):
    """Confirmed against the live login on 2026-08-17: email and PIN both go
    through, then Amazon offers to email a code. The attempt must stop there —
    it cannot read your email, and clicking 'Send verification code' would
    spam you for nothing."""
    monkeypatch.setenv("AMAZON_LOGIN_EMAIL", "someone@example.com")
    monkeypatch.setenv("AMAZON_LOGIN_PIN", "123456")
    assert relogin._needs_a_human("where should we send your verification code?")
    assert relogin._needs_a_human("check your email for the code")
    assert not relogin._needs_a_human("welcome back")


# ── a session check that does not lie (candidate) ───────────────────────────
def test_the_country_code_follows_the_site():
    assert doctor.country_code("https://hiring.amazon.ca") == "CA"
    assert doctor.country_code("https://hiring.amazon.com") == "US"


class FakeAuthRequestContext:
    def __init__(self, csrf_status=200, authorize_status=200, token="tok"):
        self.csrf_status, self.authorize_status, self.token = csrf_status, authorize_status, token
        self.posted = []

    def get(self, url, **_kw):
        page = self

        class R:
            status = page.csrf_status

            def json(self_inner):
                return {"token": page.token}
        return R()

    def post(self, url, **kw):
        self.posted.append((url, kw.get("headers", {})))
        page = self

        class R:
            status = page.authorize_status
        return R()


def test_the_authorize_probe_reports_both_outcomes():
    ok = FakeAuthRequestContext(authorize_status=200)
    assert doctor.probe_authorize(ok, "https://hiring.amazon.ca") == (200, "authenticated")

    out = FakeAuthRequestContext(authorize_status=401)
    status, meaning = doctor.probe_authorize(out, "https://hiring.amazon.ca")
    assert status == 401 and meaning == "not authenticated"


def test_the_probe_sends_the_csrf_token():
    ctx = FakeAuthRequestContext(token="abc123")
    doctor.probe_authorize(ctx, "https://hiring.amazon.ca")
    _, headers = ctx.posted[0]
    assert headers.get("anti-csrftoken-a2z") == "abc123"


def test_a_broken_probe_is_never_fatal():
    class Broken:
        def get(self, *a, **kw):
            raise RuntimeError("network is down")

    status, meaning = doctor.probe_authorize(Broken(), "https://hiring.amazon.ca")
    assert status is None
    assert "probe failed" in meaning


# ── re-login on a cycle, not on failure ─────────────────────────────────────
def _cycle_watcher(tmp_path, **overrides):
    """A watcher with the scheduled re-login armed and try_relogin stubbed."""
    overrides.setdefault("dry_run", False)
    w = _batch_watcher(tmp_path, [], **overrides)
    w.auto_relogin = True
    w.relogin_every = 6000
    w.next_relogin = 0.0
    w.attempts = []
    w.try_relogin = lambda: w.attempts.append(1) or True
    return w


def test_a_relogin_becomes_due_and_then_waits_its_turn(tmp_path):
    """The competitor's FAQ says their bot signs in every 2 hours. Ours does
    the same on a 100-minute cycle, rather than waiting to discover at 6am
    that the session died at 3."""
    w = _cycle_watcher(tmp_path)
    assert w.relogin_if_due() is True
    assert len(w.attempts) == 1
    assert w.relogin_if_due() is None, "not due again until the interval passes"


def test_no_relogin_while_a_hold_is_in_progress(tmp_path):
    """Signing in during the seconds that decide a shift would be the worst
    possible trade — the cycle can wait, the shift cannot."""
    w = _cycle_watcher(tmp_path)
    w.holding = True
    assert w.relogin_if_due() is None
    assert w.attempts == []

    w.holding = False
    assert w.relogin_if_due() is True


def test_the_cycle_is_off_in_dry_run_and_when_relogin_is_disabled(tmp_path):
    dry = _cycle_watcher(tmp_path, dry_run=True)
    assert dry.relogin_if_due() is None

    off = _cycle_watcher(tmp_path)
    off.auto_relogin = False
    assert off.relogin_if_due() is None

    unset = _cycle_watcher(tmp_path)
    unset.relogin_every = 0
    assert unset.relogin_if_due() is None


def test_a_failing_cycle_cannot_hammer_the_account(tmp_path):
    """Repeated logins are the one way this plan can do harm."""
    w = _cycle_watcher(tmp_path)
    w.max_relogins_per_day = 3
    w.try_relogin = lambda: w.attempts.append(1) or False

    for _ in range(8):
        w.next_relogin = 0.0
        w.relogin_if_due()

    assert len(w.attempts) == 3, "the daily cap has to hold"
    w.drain_notifications(timeout=5)
    assert sum("Stopped re-logging in" in t for t in w.notifier.texts) == 1


def test_the_budget_resets_the_next_day(tmp_path):
    from datetime import timedelta

    w = _cycle_watcher(tmp_path)
    w.max_relogins_per_day = 2
    w.relogins_today = 2
    w.relogin_blocked = True
    w.relogin_day = datetime.now().date() - timedelta(days=1)

    w.next_relogin = 0.0
    assert w.relogin_if_due() is True
    assert w.relogins_today == 1


def test_a_captcha_disables_the_cycle_rather_than_retrying(tmp_path, monkeypatch):
    """Retrying a CAPTCHA on a timer turns a recoverable state into a flagged
    account, and nothing here is going to solve one."""
    monkeypatch.setenv("AMAZON_LOGIN_EMAIL", "someone@example.com")
    monkeypatch.setenv("AMAZON_LOGIN_PIN", "123456")

    w = _batch_watcher(tmp_path, [], dry_run=False)
    w.auto_relogin = True
    w.relogin_every = 6000
    w.next_relogin = 0.0

    class Ctx:
        pages = []

        def new_page(self_inner):
            return FakeCheckPage("https://auth.hiring.amazon.com/#/login")

    w.context = Ctx()
    import watcher as watcher_mod

    flow = watcher_mod.login_flow          # whichever module is wired in
    monkey = lambda page, base_url, **kw: (flow.CAPTCHA, "a 'captcha' is on screen")
    original_attempt, flow.attempt = flow.attempt, monkey
    original_creds, flow.credentials = flow.credentials, lambda: ("a@b.com", "123456")
    try:
        assert w.try_relogin() is False
    finally:
        flow.attempt = original_attempt
        flow.credentials = original_creds

    assert w.relogin_blocked is True
    assert w.relogin_if_due() is None, "the cycle must stop after a CAPTCHA"
    w.drain_notifications(timeout=5)
    assert any("CAPTCHA" in t for t in w.notifier.texts)


def test_a_silly_relogin_interval_is_rejected():
    cfg = config_mod._deep_merge(
        config_mod.DEFAULTS, {"session": {"relogin_every_seconds": 60}}
    )
    with pytest.raises(ValueError, match="relogin_every_seconds"):
        config_mod.validate_config(cfg)


def test_the_shipped_config_signs_in_inside_the_observed_window():
    """Sessions were measured dying at roughly two hours, twice. A cycle
    longer than that would be a cycle that arrives late."""
    cfg = config_mod.load_config(Path(__file__).resolve().parent.parent / "config.yaml")
    every = cfg["session"]["relogin_every_seconds"]
    assert 600 <= every <= 7200, every
    assert cfg["session"]["max_relogins_per_day"] >= 86400 / every / 2


# ── the code goes to one mailbox; we might be reading another ────────────────
def test_the_code_destination_is_read_off_the_screen():
    """Amazon states it, masked: "Email verification code to t****n@gmail.com".
    Capturing it turns the commonest setup mistake — reading the wrong mailbox
    — from a silent timeout into a message that names both addresses."""
    assert relogin.code_destination(
        "Email verification code to t*********n@gmail.com"
    ) == "t*********n@gmail.com"
    assert relogin.code_destination("no address here") == ""


def test_a_masked_address_matches_its_own_mailbox():
    assert relogin.mailbox_matches("t*********n@gmail.com", "tapppamain@gmail.com") is True


def test_a_different_mailbox_is_detected():
    """The live case: codes went to tapppamain@, IMAP was reading
    singkamal2670@, and the only symptom was a 100-second wait."""
    assert relogin.mailbox_matches(
        "t*********n@gmail.com", "singkamal2670@gmail.com"
    ) is False


def test_a_different_domain_is_a_mismatch():
    assert relogin.mailbox_matches("t*****n@gmail.com", "tapppamain@outlook.com") is False


def test_an_unjudgeable_pair_says_so_rather_than_guessing():
    """Forwarding makes a mismatch perfectly workable, so this only ever
    warns — and with nothing to compare it must not claim a verdict."""
    assert relogin.mailbox_matches("", "someone@gmail.com") is None
    assert relogin.mailbox_matches("t***n@gmail.com", "") is None
    assert relogin.mailbox_matches("not-an-address", "someone@gmail.com") is None


def test_an_app_password_pasted_with_spaces_still_works(monkeypatch):
    """Google displays them as "abcd efgh ijkl mnop" and that is how people
    paste them. Google ignores the spaces; a stricter server might not."""
    monkeypatch.setenv("OTP_IMAP_USER", "someone@gmail.com")
    monkeypatch.setenv("OTP_IMAP_PASSWORD", "abcd efgh ijkl mnop")
    host, user, password = otp_mail.configured()
    assert password == "abcdefghijklmnop"
    assert host == "imap.gmail.com"


def test_only_a_code_email_can_supply_a_code():
    """Found live: a marketing email in the same inbox — "You're on the list.
    Welcome to job alerts." — yielded a plausible six-digit number. Typing a
    wrong code costs the whole attempt, since the real one expires in three
    minutes and resend is blocked for 55 seconds."""
    assert otp_mail.subject_is_a_code_email("Your Amazon Jobs verification code")
    assert otp_mail.subject_is_a_code_email("Your security code")
    assert not otp_mail.subject_is_a_code_email(
        "You’re on the list. Welcome to job alerts."
    )
    assert not otp_mail.subject_is_a_code_email("Warehouse jobs near you")
    assert not otp_mail.subject_is_a_code_email("")


def test_the_code_is_read_out_of_amazons_real_wording():
    """Wording taken from the live email, not invented for the test."""
    body = (
        "Your Amazon Jobs verification code\n"
        "Use this code to verify your account: 485469\n"
        "It will expire in 3 minutes.\n"
        "Job ID: JOB-CA-0000123456\n"
        "© 1996-2026 Amazon.com, Inc."
    )
    assert otp_mail.extract_code(body) == "485469"


def test_a_job_id_is_not_mistaken_for_a_code():
    assert otp_mail.extract_code("Job-CA-0000123456 was posted") is None


# ── the CAPTCHA that actually stops the re-login ────────────────────────────
def test_amazons_real_captcha_wording_is_recognised():
    """CONFIRMED live 2026-08-18: clicking "Send verification code" produced
    "Let's confirm you are human / Choose all the clocks". The marker list said
    "verify you are human", which missed it — so four attempts sat behind a
    CAPTCHA that stopped the mail being sent, and blamed the mailbox."""
    assert relogin._is_captcha("let's confirm you are human")
    assert relogin._is_captcha("choose all the clocks")
    assert relogin._is_captcha("select each image with a bus")


def test_a_code_challenge_is_still_not_a_captcha():
    """The two must never be confused: one can be finished from the mailbox,
    the other never can."""
    assert relogin._is_captcha("enter the verification code we sent") is None
    assert relogin._needs_a_human("enter the verification code we sent")


class CaptchaPage:
    """A page whose CAPTCHA is structural rather than worded."""

    def __init__(self, has_widget):
        self.has_widget = has_widget

    def locator(self, selector):
        page = self

        class L:
            @property
            def first(self_inner):
                return self_inner

            def count(self_inner):
                return 1 if page.has_widget and "captcha" in selector.lower() else 0

        return L()


def test_a_captcha_widget_is_caught_even_if_the_wording_changes():
    assert relogin.captcha_on_screen(CaptchaPage(True)) is True
    assert relogin.captcha_on_screen(CaptchaPage(False)) is False


def test_a_dead_session_alerts_instead_of_attempting_a_doomed_hold(tmp_path):
    """From the Etobicoke miss: with a dead session the schedule flyout never
    renders an Apply button, so the attempt spent 11.5 seconds timing out
    before reporting failure. Those are the only seconds that matter."""
    shipped = _shipped()
    w = _batch_watcher(
        tmp_path,
        [Shift(id="1", title="Amazon Delivery Station Warehouse Associate",
               location="Etobicoke, ON", pay_rate=23.10,
               url="https://hiring.amazon.ca/app#/jobDetail?jobId=JOB-CA-1")],
        dry_run=False,
        filters=shipped["filters"],
        priority=shipped["priority"],
    )
    w.session_ok = False
    attempted = []
    w.holding_attempts = attempted

    original = site_selectors.hold_shift
    site_selectors.hold_shift = lambda *a, **kw: attempted.append(1) or (False, "should not run")
    try:
        w.poll_once()
    finally:
        site_selectors.hold_shift = original
    w.drain_notifications(timeout=5)

    assert attempted == [], "no hold should be attempted with a dead session"
    urgent = [t for t in w.notifier.texts if "session is dead" in t.lower()]
    assert urgent, w.notifier.texts
    assert "jobDetail" in urgent[0], "the alert must carry the link to grab by hand"
    assert "save_session.py" in urgent[0]


def test_amazons_no_pay_sentinel_is_not_a_wage():
    """Seen live: a Nisku posting came back with 1.797e308 — DBL_MAX, Amazon's
    "no pay data" marker. Parsed as a wage it sorts above every real shift, so
    priority.order: pay would rank it first in a batch."""
    assert Shift(pay_rate=1.7976931348623157e308).pay_rate is None
    assert Shift(pay_rate=float("inf")).pay_rate is None
    assert Shift(pay_rate=99999).pay_rate is None
    # ...while real wages are untouched
    assert Shift(pay_rate=23.10).pay_rate == 23.10
    assert Shift(pay_rate="$18.50/hr").pay_rate == 18.50


def test_a_sentinel_pay_rate_ranks_last_not_first():
    ranker = ShiftRanker({"order": ["pay"], "locations": [], "titles": [], "demote_titles": []})
    junk = Shift(id="junk", title="A", location="Nisku, AB", pay_rate=1.7976931348623157e308)
    real = Shift(id="real", title="B", location="Brampton, ON", pay_rate=23.10)
    assert ranker.sort([junk, real])[0].id == "real"


# ── choosing WHICH schedule, not just the first one ─────────────────────────
CARD_A = "$24.00/hr | Featured | Schedule (26h per week) | Wed, Thu, Fri, Sat 9:30 PM - 4:00 AM"
CARD_B = "$24.00/hr | Featured | Schedule (26h per week) | Sun, Mon, Tue, Wed 9:30 PM - 4:00 AM"


def test_a_schedule_card_is_parsed_from_its_visible_text():
    """Wording taken from the live flyout, not invented."""
    card = schedules_mod.parse_card_text(CARD_A)
    assert card["hours_per_week"] == 26.0
    assert card["pay_rate"] == 24.0
    assert card["days"] == ["wed", "thu", "fri", "sat"]
    assert card["starts"] == "9:30 PM" and card["ends"] == "4:00 AM"


def test_a_shift_running_past_midnight_is_overnight():
    assert schedules_mod.is_overnight(schedules_mod.parse_card_text(CARD_A)) is True
    day = schedules_mod.parse_card_text("Mon, Tue 7:00 AM - 3:30 PM")
    assert schedules_mod.is_overnight(day) is False


def test_availability_decides_between_otherwise_identical_schedules():
    """The live case: same pay, same hours, different days. Whichever renders
    first is an accident, and you have to work whichever one it takes."""
    cards = [schedules_mod.parse_card_text(CARD_A), schedules_mod.parse_card_text(CARD_B)]
    index, why = schedules_mod.choose_card(cards, {"available_days": ["sun", "mon", "tue", "wed"]})
    assert index == 1, why
    index, _ = schedules_mod.choose_card(cards, {"available_days": ["wed", "thu", "fri", "sat"]})
    assert index == 0


def test_a_schedule_you_can_only_half_work_is_refused():
    card = schedules_mod.parse_card_text(CARD_A)
    ok, reason = schedules_mod.card_is_acceptable(card, {"available_days": ["wed", "thu"]})
    assert not ok
    assert "fri" in reason and "sat" in reason


def test_minimum_hours_and_overnight_are_enforced():
    card = schedules_mod.parse_card_text(CARD_A)
    ok, reason = schedules_mod.card_is_acceptable(card, {"min_hours_per_week": 30})
    assert not ok and "below" in reason

    ok, reason = schedules_mod.card_is_acceptable(card, {"avoid_overnight": True})
    assert not ok and "overnight" in reason


def test_no_acceptable_schedule_says_why_rather_than_picking_one():
    cards = [schedules_mod.parse_card_text(CARD_A), schedules_mod.parse_card_text(CARD_B)]
    index, why = schedules_mod.choose_card(cards, {"min_hours_per_week": 40})
    assert index is None
    assert "below" in why


def test_with_no_preferences_the_first_schedule_still_wins():
    """Unchanged behaviour when nothing is configured — the feature must not
    quietly start making choices for someone who never asked it to."""
    cards = [schedules_mod.parse_card_text(CARD_A), schedules_mod.parse_card_text(CARD_B)]
    assert schedules_mod.choose_card(cards, None)[0] == 0
    assert schedules_mod.choose_card(cards, {})[0] == 0


# ── the fallback cascade: next schedule, then next city ─────────────────────
def test_a_sniped_slot_is_worth_another_schedule():
    """The competitor frequently takes the slot between the flyout rendering
    and the Apply landing. The next schedule on the same job is far cheaper to
    try than another job in another city."""
    sniped = site_selectors.HoldResult(
        site_selectors.FAILED, "hold failed at step 2 (pick a shift): Timeout"
    )
    assert sniped.worth_retrying() is True


def test_a_dead_session_is_not_worth_retrying():
    """It would fail identically three times while the posting disappears."""
    for message in (
        "the apply flow needs a login (opened https://auth.hiring.amazon.com)",
        "selectors not configured: FINAL_SUBMIT",
        "no schedule left to try: 2 acceptable of 2 on offer",
        "could not find the card for 'x' on the page",
    ):
        result = site_selectors.HoldResult(site_selectors.FAILED, message)
        assert result.worth_retrying() is False, message


def test_a_success_is_never_retried():
    for status in (site_selectors.CONFIRMED, site_selectors.UNCERTAIN):
        assert site_selectors.HoldResult(status, "done").worth_retrying() is False


def test_ranked_order_is_what_gets_tried_in_sequence():
    """Attempt 0 takes the best schedule, attempt 1 the next best — not the
    flyout's render order."""
    cards = [
        schedules_mod.parse_card_text("$20.00/hr | Schedule (20h per week) | Mon 7:00 AM - 12:00 PM"),
        schedules_mod.parse_card_text("$26.00/hr | Schedule (40h per week) | Tue 7:00 AM - 3:30 PM"),
        schedules_mod.parse_card_text("$23.00/hr | Schedule (30h per week) | Wed 7:00 AM - 1:30 PM"),
    ]
    order = schedules_mod.rank_cards(cards, None)
    assert order == [1, 2, 0], "best pay first, then hours, then render order"


def test_the_shipped_config_retries_but_within_a_budget():
    cfg = config_mod.load_config(Path(__file__).resolve().parent.parent / "config.yaml")
    assert cfg["hold"]["schedule_attempts"] >= 2
    assert 10 <= cfg["hold"]["attempt_budget_seconds"] <= 120, (
        "a posting lasts about a minute — an unbounded retry loop spends it"
    )


# ── Amazon's own preference model: warehouse type and shift type ────────────
def test_the_four_warehouse_types_are_recognised():
    """From the onboarding flow: Delivery Station, Fulfillment Centre,
    Sortation Centre, XL Warehouse."""
    assert warehouse_type("Delivery Station Warehouse Associate") == "delivery station"
    assert warehouse_type("Fulfillment Center Warehouse Associate") == "fulfillment centre"
    assert warehouse_type("Sortation Centre Warehouse Associate") == "sortation centre"
    assert warehouse_type("XL Warehouse Associate") == "xl warehouse"
    assert warehouse_type("Robotics Warehouse Associate") == ""


def test_delivery_station_is_an_associate_job_not_a_driving_one():
    """It was demoted on the assumption it needed a licence. It does not — it
    is the same associate role in a different building type, and Amazon's own
    onboarding lists it alongside the other three."""
    cfg = config_mod.load_config(Path(__file__).resolve().parent.parent / "config.yaml")
    assert cfg["priority"]["demote_titles"] == []

    ranker = ShiftRanker(cfg["priority"])
    delivery_close = Shift(id="1", title="Delivery Station Warehouse Associate",
                           location="Brampton, ON", pay_rate=23.10)
    fulfilment_far = Shift(id="2", title="Fulfillment Center Warehouse Associate",
                           location="Toronto, ON", pay_rate=23.50)
    assert ranker.sort([fulfilment_far, delivery_close])[0].id == "1", (
        "with location first, the closer Delivery job should now win"
    )


def test_warehouse_types_filter_when_set_and_are_open_when_not():
    everything = ShiftMatcher({})
    assert everything.matches(Shift(title="Sortation Centre Warehouse Associate"))[0]

    picky = ShiftMatcher({"warehouse_types": ["delivery station", "fulfillment centre"]})
    assert picky.matches(Shift(title="Delivery Station Warehouse Associate"))[0]
    ok, reason = picky.matches(Shift(title="Sortation Centre Warehouse Associate"))
    assert not ok and "sortation centre" in reason


def test_shift_types_filter_on_what_the_api_actually_returns():
    """jobType arrives as PART_TIME / FULL_TIME / FLEX_TIME / REDUCED_TIME."""
    picky = ShiftMatcher({"shift_types": ["part time", "flex time"]})
    assert picky.matches(Shift(title="A", schedule="PART_TIME"))[0]
    assert picky.matches(Shift(title="A", schedule="PART_TIME;REDUCED_TIME"))[0]
    ok, reason = picky.matches(Shift(title="A", schedule="FULL_TIME"))
    assert not ok and "FULL_TIME" in reason


# ── place names must match whole words ──────────────────────────────────────
def test_milton_does_not_match_hamilton():
    """Found the moment the GTA list was widened: "milton" is inside
    "Hamilton", so a substring match accepted a posting 70km away."""
    matcher = ShiftMatcher(_shipped()["filters"])
    assert matcher.matches(_gta(FULFILLMENT, "Milton"))[0]
    assert not matcher.matches(_gta(FULFILLMENT, "Hamilton"))[0]


def test_the_province_excludes_still_work_after_the_word_boundaries():
    """A leading word-boundary guard on ", bc" would demand a non-letter before
    the comma — never true — and would silently disable every province
    exclude. That regression lasted one test run."""
    matcher = ShiftMatcher(_shipped()["filters"])
    assert matcher.matches(_gta(FULFILLMENT, "Maple"))[0]
    assert not matcher.matches(Shift(title=FULFILLMENT, location="Maple Ridge, BC"))[0]
    assert not matcher.matches(Shift(title=FULFILLMENT, location="Nisku, AB"))[0]


def test_titles_still_match_on_substrings():
    """Deliberately different from locations. Amazon spells it both ways —
    "Fulfillment Center" on the US site, "Fulfilment Centre" on the CA one —
    and a partial needle has to catch both, which whole-word matching would
    not."""
    matcher = ShiftMatcher({"include_titles": ["fulfillment cent"]})
    assert matcher.matches(Shift(title="Fulfillment Center Warehouse Associate"))[0]
    assert matcher.matches(Shift(title="Fulfillment Centre Warehouse Associate"))[0]


def test_the_gta_list_covers_torontos_districts():
    """Amazon labels sites by district as often as by city, so matching
    "toronto" alone would miss Etobicoke, Scarborough and North York."""
    matcher = ShiftMatcher(_shipped()["filters"])
    for district in ("Etobicoke", "Scarborough", "North York", "East York"):
        assert matcher.matches(_gta(FULFILLMENT, district))[0], district


def test_the_gta_list_stops_at_the_radius():
    matcher = ShiftMatcher(_shipped()["filters"])
    for far in ("Ottawa", "Barrie", "London", "Kingston", "Windsor"):
        assert not matcher.matches(_gta(FULFILLMENT, far))[0], far


# ── falling through to the next job, not just the next schedule ─────────────
def _failing_hold(results):
    """A _hold stand-in that returns queued outcomes in order."""
    queue = list(results)
    calls = []

    def hold(shift, **_kw):
        calls.append(shift)
        return queue.pop(0) if queue else site_selectors.HoldResult(
            site_selectors.FAILED, "hold failed at step 2 (pick a shift): Timeout"
        )

    hold.calls = calls
    return hold


def _sniped():
    return site_selectors.HoldResult(
        site_selectors.FAILED, "hold failed at step 2 (pick a shift): Timeout"
    )


def _confirmed():
    return site_selectors.HoldResult(site_selectors.CONFIRMED, "SPOT HELD")


def test_a_sniped_job_falls_through_to_the_next_one(tmp_path):
    """Asked for directly: "go to the other city". Losing the first job to a
    faster service should cost that job, not the whole batch."""
    w = _batch_watcher(tmp_path, _many(4), dry_run=False)
    w._hold = _failing_hold([_sniped(), _sniped(), _confirmed()])
    w.poll_once()
    assert len(w._hold.calls) == 3, "should have walked down to the third job"


def test_it_stops_at_the_first_job_that_sticks(tmp_path):
    w = _batch_watcher(tmp_path, _many(4), dry_run=False)
    w._hold = _failing_hold([_confirmed()])
    w.poll_once()
    assert len(w._hold.calls) == 1, "no reason to hold a second shift"


def test_a_dead_session_stops_the_cascade_immediately(tmp_path):
    """Every other job in the batch would fail identically, and the batch
    lasts about a minute."""
    dead = site_selectors.HoldResult(
        site_selectors.FAILED, "the session needs a login before anything can be held"
    )
    w = _batch_watcher(tmp_path, _many(4), dry_run=False)
    w._hold = _failing_hold([dead])
    w.poll_once()
    assert len(w._hold.calls) == 1


def test_the_cascade_respects_job_attempts(tmp_path):
    w = _batch_watcher(tmp_path, _many(9), dry_run=False, hold={"job_attempts": 2})
    w._hold = _failing_hold([_sniped(), _sniped(), _sniped()])
    w.poll_once()
    assert len(w._hold.calls) == 2


def test_an_uncertain_hold_counts_and_is_not_retried(tmp_path):
    """"Pressed Create Application but never saw the banner" may well be a
    held shift. Trying the next job could book you two."""
    maybe = site_selectors.HoldResult(
        site_selectors.UNCERTAIN, "clicked but the banner never appeared"
    )
    w = _batch_watcher(tmp_path, _many(4), dry_run=False)
    w._hold = _failing_hold([maybe])
    w.poll_once()
    assert len(w._hold.calls) == 1


def test_the_country_is_resolved_for_both_sites():
    """A typo in COUNTRY_BY_HOST ("hirring") made every lookup miss, and
    country_for falls back to Canada — so the US login silently picked the
    wrong country and failed at a step that looks nothing like a typo."""
    assert relogin.country_for("https://hiring.amazon.ca") == "Canada"
    assert relogin.country_for("https://hiring.amazon.com") == "United States"
    assert set(relogin.COUNTRY_BY_HOST) == {"hiring.amazon.ca", "hiring.amazon.com"}


def test_every_configured_site_resolves_to_a_country():
    """The real guard: whatever base_url the configs carry must be found in
    the map, not silently defaulted."""
    root = Path(__file__).resolve().parent.parent
    for name, expected in (("config.yaml", "Canada"), ("config.us.yaml", "United States")):
        base = config_mod.load_config(root / name)["site"]["base_url"]
        assert any(host in base for host in relogin.COUNTRY_BY_HOST), base
        assert relogin.country_for(base) == expected, name


def test_startup_does_not_count_as_an_overdue_relogin(tmp_path):
    """Seen live: next_relogin started at 0.0, which reads as "due now", so a
    scheduled login fired 17ms after an expiry-triggered one had just failed —
    two attempts in the same second, two solver calls, two codes emailed."""
    w = _batch_watcher(tmp_path, [], dry_run=False, session={
        "auto_relogin": True, "relogin_every_seconds": 6000,
    })
    assert w.relogin_due() is False, "nothing is overdue at startup"


def test_any_attempt_postpones_the_scheduled_one(tmp_path):
    """The expiry path and the timer must not fire back to back."""
    w = _batch_watcher(tmp_path, [], dry_run=False, session={
        "auto_relogin": True, "relogin_every_seconds": 6000,
    })
    w.next_relogin = 0.0                      # pretend the timer is due
    assert w.relogin_due() is True

    import watcher as watcher_mod

    flow = watcher_mod.login_flow
    original, flow.credentials = flow.credentials, lambda: ("a@b.com", "123456")
    try:
        w.try_relogin()                       # triggered from anywhere
    finally:
        flow.credentials = original
    assert w.relogin_due() is False, "an attempt must restart the clock"


def test_a_failed_relogin_captures_what_was_on_screen(tmp_path, caplog):
    """A status word is not a diagnosis. "failed at OTP_ENTRY_REQUIRED" says
    where the state machine gave up, not what Amazon was showing — a rejected
    code, a fresh challenge and a silent timeout all read identically."""
    w = _batch_watcher(tmp_path, [], dry_run=False)

    class DeadEndPage:
        url = "https://auth.hiring.amazon.com/#/login"

        def screenshot(self, **kw):
            Path(kw["path"]).write_bytes(b"png")

        def inner_text(self, _sel):
            return "Enter the verification code\nThat code is incorrect"

    with caplog.at_level("ERROR"):
        w._capture_relogin_failure(DeadEndPage(), "unknown")

    assert "auth.hiring.amazon.com" in caplog.text
    assert "That code is incorrect" in caplog.text


def test_failure_capture_never_breaks_the_watcher(tmp_path):
    """Diagnostics are the last thing that should take a run down."""
    w = _batch_watcher(tmp_path, [], dry_run=False)

    class Hostile:
        url = property(lambda self: (_ for _ in ()).throw(RuntimeError("gone")))

        def screenshot(self, **kw):
            raise RuntimeError("no screen")

        def inner_text(self, _sel):
            raise RuntimeError("no body")

    w._capture_relogin_failure(Hostile(), "unknown")   # must not raise

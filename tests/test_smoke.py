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
import browser_launch
import config as config_mod
import drop_report
import site_selectors
from notifier import TelegramNotifier
from shift_matcher import Shift, ShiftMatcher
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
    assert cfg["dry_run"] is True, "shipped config must stay safe by default"
    assert cfg["hold"]["stop_before_submit"] is True
    assert cfg["polling"]["mode"] in ("dom", "api")


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
    monkeypatch.setattr(site_selectors, "FINAL_SUBMIT", "#submit")
    monkeypatch.setattr(site_selectors, "HOLD_STEPS", [("open job", ":scope"), ("sched", "#s")])
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
    ok, msg = site_selectors.hold_shift(Page(), Shift(id="X", title="t"))
    assert not searched, "must not hunt for a card on the detail page"
    assert ok, msg


def test_no_results_selector_is_configured_from_the_live_site():
    """Confirmed live: <div id="jobNotFoundContainer">. Without this, 'no jobs
    posted' and 'our selectors rotted' are indistinguishable."""
    assert site_selectors.SELECTORS["no_results"] == "#jobNotFoundContainer"
    assert "no_results" not in site_selectors.unconfigured()


def test_polling_defaults_are_conservative_enough_for_the_waf():
    cfg = config_mod.load_config(Path(__file__).resolve().parent.parent / "config.yaml")
    assert cfg["polling"]["interval_seconds"] >= 30, (
        "a live test got CloudFront-blocked at ~14s between page loads"
    )
    assert cfg["polling"]["render_wait_ms"] >= 1000


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


def test_remaining_placeholders_are_reported():
    """Detection selectors are captured; the final apply steps are not, because
    capturing them would mean submitting a real application."""
    missing = site_selectors.unconfigured()
    assert "FINAL_SUBMIT" in missing
    assert any("pick a shift" in m for m in missing)
    assert any("create application" in m for m in missing)
    # ...but nothing needed for *detection* is still a placeholder.
    assert not any(m.startswith("card_") or m == "job_card" for m in missing)
    assert not site_selectors.selectors_ready()


def test_detection_can_be_ready_while_holding_is_not():
    """Regression: the watcher used to refuse to start in dom mode unless every
    selector was filled in, including the apply-flow steps that can only be
    captured by submitting a real application. That blocked the dry-run period
    you are supposed to do *first*."""
    assert site_selectors.detection_ready()
    assert not site_selectors.selectors_ready()
    assert site_selectors.unconfigured_hold()
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


def test_hold_shift_refuses_when_selectors_are_placeholders():
    ok, message = site_selectors.hold_shift(FakePage([]), Shift(title="x"))
    assert not ok
    assert "not configured" in message


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

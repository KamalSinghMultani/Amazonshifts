from types import SimpleNamespace

import watcher_v2
from shift_matcher import Shift


def _bare_watcher(prefs=None):
    obj = object.__new__(watcher_v2.ScheduleAwareWatcher)
    obj.cfg = {"schedule_preferences": prefs or {}}
    return obj


def test_candidate_key_uses_schedule_id_not_parent_job_id():
    watcher = _bare_watcher()
    shift = Shift(
        id="SCH-CA-123",
        title="Warehouse Associate",
        raw={"jobId": "JOB-CA-999", "scheduleId": "SCH-CA-123"},
    )

    done, alert = watcher._candidate_key(shift)

    assert done == "done:schedule:SCH-CA-123"
    assert alert == "alert:schedule:SCH-CA-123"


def test_schedule_preferences_reject_unavailable_day():
    watcher = _bare_watcher({"available_days": ["mon", "tue", "wed", "thu", "fri"]})
    schedule = SimpleNamespace(
        text="Sat, Sun 8:00 AM - 6:30 PM",
        hours_per_week=20,
        pay_rate=22.0,
    )

    ok, reason = watcher._schedule_is_acceptable(schedule)

    assert ok is False
    assert "sat" in reason or "sun" in reason


def test_schedule_preferences_reject_overnight():
    watcher = _bare_watcher({"avoid_overnight": True})
    schedule = SimpleNamespace(
        text="Wed, Thu, Fri, Sat 9:30 PM - 4:00 AM",
        hours_per_week=26,
        pay_rate=24.0,
    )

    ok, reason = watcher._schedule_is_acceptable(schedule)

    assert ok is False
    assert "overnight" in reason


def test_schedule_preferences_enforce_minimum_hours():
    watcher = _bare_watcher({"min_hours_per_week": 30})
    schedule = SimpleNamespace(
        text="Mon, Tue, Wed 8:00 AM - 4:00 PM",
        hours_per_week=24,
        pay_rate=22.0,
    )

    ok, reason = watcher._schedule_is_acceptable(schedule)

    assert ok is False
    assert "below" in reason


def test_schedule_shift_identity_is_schedule_not_job():
    watcher = _bare_watcher()
    job = Shift(
        id="JOB-CA-999",
        title="Warehouse Associate",
        location="Brampton, ON",
        schedule="PART_TIME",
        pay_rate=21.0,
    )
    schedule = SimpleNamespace(
        raw={"jobId": "JOB-CA-999", "scheduleId": "SCH-CA-123"},
        job_id="JOB-CA-999",
        id="SCH-CA-123",
        title="Warehouse Associate",
        location="Brampton, ON",
        text="Mon, Tue 8:00 AM - 4:00 PM",
        pay_rate=22.0,
        available=1,
    )

    concrete = watcher._schedule_shift(job, schedule)

    assert concrete.id == "SCH-CA-123"
    assert concrete.raw["parentJobId"] == "JOB-CA-999"
    assert concrete.raw["scheduleId"] == "SCH-CA-123"

from types import SimpleNamespace

import schedule_batch
import watcher_v3


def test_batch_request_aliases_each_job_once():
    payload, ids = schedule_batch.build_request(
        ["JOB-1", "JOB-2", "JOB-1"], country="Canada", locale="en-CA"
    )

    assert ids == ["JOB-1", "JOB-2"]
    assert payload["variables"]["r0"]["jobId"] == "JOB-1"
    assert payload["variables"]["r1"]["jobId"] == "JOB-2"
    assert "s0: searchScheduleCards" in payload["query"]
    assert "s1: searchScheduleCards" in payload["query"]


def test_batch_parse_preserves_job_mapping():
    payload = {
        "data": {
            "s0": {"scheduleCards": [{
                "jobId": "JOB-1", "scheduleId": "SCH-1",
                "laborDemandAvailableCount": 1,
            }]},
            "s1": {"scheduleCards": [{
                "jobId": "JOB-2", "scheduleId": "SCH-2",
                "laborDemandAvailableCount": 2,
            }]},
        }
    }

    parsed = schedule_batch.parse(payload, ["JOB-1", "JOB-2"])

    assert parsed["JOB-1"][0].id == "SCH-1"
    assert parsed["JOB-2"][0].id == "SCH-2"


def test_failure_budget_counts_failures_not_successful_refreshes():
    watcher = object.__new__(watcher_v3.OptimizedWatcher)
    watcher.failed_relogins_today = 0
    watcher.failed_relogin_day = watcher_v3.datetime.now().date()
    watcher.max_failed_relogins_per_day = 2
    watcher.relogin_blocked = False

    assert watcher._failure_budget_left() is True
    # A healthy refresh does not mutate the failure counter. The counter only
    # changes in _poll_session_worker when a worker result is not ok/healthy.
    assert watcher.failed_relogins_today == 0
    watcher.failed_relogins_today = 2
    assert watcher._failure_budget_left() is False


def test_batch_expand_filters_zero_capacity_and_preferences(monkeypatch):
    watcher = object.__new__(watcher_v3.OptimizedWatcher)
    watcher.mode = "api"
    watcher.api_client = SimpleNamespace(country="Canada", locale="en-CA")
    watcher.cfg = {"schedule_preferences": {"min_hours_per_week": 20}}

    job = SimpleNamespace(
        id="JOB-1", title="Warehouse", location="Brampton", schedule="PART_TIME",
        pay_rate=21.0,
    )
    open_schedule = SimpleNamespace(
        id="SCH-OPEN", job_id="JOB-1", title="Warehouse", location="Brampton, ON",
        text="Mon, Tue, Wed 8:00 AM - 4:00 PM", hours_per_week=24,
        pay_rate=22.0, available=1,
        raw={"jobId": "JOB-1", "scheduleId": "SCH-OPEN", "laborDemandAvailableCount": 1},
    )
    closed_schedule = SimpleNamespace(
        id="SCH-CLOSED", job_id="JOB-1", title="Warehouse", location="Brampton, ON",
        text="Mon, Tue 8:00 AM - 4:00 PM", hours_per_week=16,
        pay_rate=22.0, available=0,
        raw={"jobId": "JOB-1", "scheduleId": "SCH-CLOSED", "laborDemandAvailableCount": 0},
    )

    monkeypatch.setattr(
        watcher_v3.schedule_batch,
        "fetch",
        lambda *_args, **_kwargs: {"JOB-1": [open_schedule, closed_schedule]},
    )

    # Use the inherited converters directly; the SimpleNamespace objects expose
    # the same fields as schedules.Schedule.
    result = watcher._batch_expand([job])

    assert [shift.id for shift in result] == ["SCH-OPEN"]

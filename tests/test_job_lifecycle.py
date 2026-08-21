from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import job_lifecycle
import notifier
import schedules
import watcher_v7


def _known():
    return [
        {"site_code": "YYZ4", "location": "Brampton, ON", "job_id": "JOB-1"},
        {"site_code": "DTY4", "location": "Mississauga, ON", "job_id": "JOB-2"},
    ]


def _detail(job_id, status="POSTED"):
    return {
        "jobId": job_id,
        "postingStatus": status,
        "locationName": "Brampton, ON",
        "siteId": ["SITE-YYZ4"],
        "jobTitle": "Amazon Fulfillment Centre Warehouse Associate",
        "employmentType": "Seasonal Regular",
    }


def _schedule(capacity=1, schedule_id="SCH-1"):
    return schedules.Schedule({
        "jobId": "JOB-1",
        "scheduleId": schedule_id,
        "externalJobTitle": "Amazon Fulfillment Centre Warehouse Associate",
        "city": "Brampton",
        "state": "ON",
        "siteId": "YYZ4",
        "scheduleText": "Sun, Mon, Tue, Wed 3:00 PM - 8:30 PM",
        "hoursPerWeek": 22,
        "totalPayRate": 25.60,
        "laborDemandAvailableCount": capacity,
    })


def test_job_detail_request_uses_public_ids_and_frontend_shape():
    payload = job_lifecycle.build_request("JOB-1")
    assert payload["variables"]["getJobDetailRequest"] == {
        "jobId": "JOB-1", "locale": "en-CA"
    }
    assert payload["operationName"] == "getJobDetail"
    assert payload["query"].startswith(
        "query getJobDetail($getJobDetailRequest: GetJobDetailRequest!)"
    )
    assert "batchJobDetail" not in payload["query"]
    assert "GetJobDetailRequest!" in payload["query"]
    assert "getJobDetail(getJobDetailRequest: $getJobDetailRequest)" in payload["query"]
    assert "j0:" not in payload["query"]
    assert "postingStatus" in payload["query"]
    serialized = json.dumps(payload).lower()
    for secret_name in ("authorization", "cookie", "token", "otp", "pin", "trackingid"):
        assert secret_name not in serialized


def test_parse_preserves_missing_as_inconclusive():
    result = job_lifecycle.parse({"data": {"getJobDetail": _detail("JOB-1")}})
    assert result["postingStatus"] == "POSTED"
    assert job_lifecycle.parse({"data": {"getJobDetail": None}}) is None


def test_one_job_graphql_error_does_not_discard_other_valid_jobs():
    class Client:
        locale = "en-CA"

        def _post_json(self, request):
            job_id = request["variables"]["getJobDetailRequest"]["jobId"]
            if job_id == "JOB-1":
                return {"data": {"getJobDetail": None}, "errors": [{"message": "hidden"}]}
            return {"data": {"getJobDetail": _detail(job_id, "UNPOSTED")}}

    result = job_lifecycle.fetch(Client(), [
        job_lifecycle.KnownJob("JOB-1"),
        job_lifecycle.KnownJob("JOB-2"),
    ])

    assert result["JOB-1"] is None
    assert result["JOB-2"]["postingStatus"] == "UNPOSTED"


def test_lifecycle_round_robin_samples_each_known_id_without_a_burst(monkeypatch, tmp_path):
    monitor = job_lifecycle.LifecycleMonitor(
        _known(), state_path=tmp_path / "state.json", events_path=tmp_path / "events.jsonl"
    )
    sampled = []

    def fetch_sample(_client, jobs):
        sampled.append([job.job_id for job in jobs])
        return {job.job_id: _detail(job.job_id, "UNPOSTED") for job in jobs}

    monkeypatch.setattr(job_lifecycle, "fetch", fetch_sample)

    monitor.poll(object(), max_jobs=1)
    monitor.poll(object(), max_jobs=1)
    monitor.poll(object(), max_jobs=1)

    assert sampled == [["JOB-1"], ["JOB-2"], ["JOB-1"]]
    assert monitor.last_attempted_jobs == 1
    assert monitor.last_observed_jobs == 1


def test_config_checks_only_brampton_and_mississauga_ids_each_pass():
    import yaml

    cfg = yaml.safe_load(open("config.yaml", encoding="utf-8"))
    lifecycle = cfg["lifecycle_monitor"]

    assert len(lifecycle["known_jobs"]) == 5
    assert lifecycle["jobs_per_poll"] == 5
    assert lifecycle["interval_seconds"] == 2.0
    assert {
        item["location"] for item in lifecycle["known_jobs"]
    } == {"Brampton, ON", "Mississauga, ON"}


def test_only_posted_plus_strict_positive_capacity_emits_candidate(monkeypatch, tmp_path):
    monitor = job_lifecycle.LifecycleMonitor(
        _known(), state_path=tmp_path / "state.json", events_path=tmp_path / "events.jsonl"
    )
    monkeypatch.setattr(
        job_lifecycle, "fetch", lambda *_a, **_k: {
            "JOB-1": _detail("JOB-1", "POSTED"),
            "JOB-2": _detail("JOB-2", "UNPOSTED"),
        },
    )
    monkeypatch.setattr(
        job_lifecycle.schedule_batch, "fetch",
        lambda *_a, **_k: {"JOB-1": [_schedule(0, "ZERO"), _schedule(None, "UNKNOWN"), _schedule(2, "OPEN")]},
    )

    candidates, events = monitor.poll(object())
    assert monitor.last_observed_jobs == 2
    assert [candidate.id for candidate in candidates] == ["OPEN"]
    assert candidates[0].raw["postingStatus"] == "POSTED"
    assert candidates[0].raw["laborDemandAvailableCount"] == 2
    assert {event["event"] for event in events} == {"JOB_POSTED", "SCHEDULE_CAPACITY_AVAILABLE"}


def test_same_pair_rearms_after_posted_unposted_posted_transition(monkeypatch, tmp_path):
    current = [datetime(2026, 8, 21, 1, 0, tzinfo=timezone.utc)]
    statuses = iter(["POSTED", "UNPOSTED", "POSTED"])
    monitor = job_lifecycle.LifecycleMonitor(
        [_known()[0]], state_path=tmp_path / "state.json", events_path=tmp_path / "events.jsonl",
        now_fn=lambda: current[0],
    )
    monkeypatch.setattr(
        job_lifecycle, "fetch",
        lambda *_a, **_k: {"JOB-1": _detail("JOB-1", next(statuses))},
    )
    monkeypatch.setattr(
        job_lifecycle.schedule_batch, "fetch", lambda *_a, **_k: {"JOB-1": [_schedule(1)]},
    )

    first, _ = monitor.poll(object())
    first_epoch = first[0].raw["lifecycleEpoch"]
    current[0] += timedelta(seconds=10)
    middle, events = monitor.poll(object())
    assert middle == []
    assert events[0]["event"] == "JOB_UNPOSTED"
    assert events[0]["postedDurationSeconds"] == 10
    current[0] += timedelta(seconds=5)
    second, _ = monitor.poll(object())
    assert second[0].raw["lifecycleEpoch"] != first_epoch

    lines = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text("utf-8").splitlines()]
    assert [line["event"] for line in lines] == [
        "JOB_POSTED", "SCHEDULE_CAPACITY_AVAILABLE", "JOB_UNPOSTED",
        "JOB_POSTED", "SCHEDULE_CAPACITY_AVAILABLE",
    ]
    assert "tracking" not in (tmp_path / "events.jsonl").read_text("utf-8").lower()


def test_capacity_rising_edge_inside_one_posted_window_is_recorded(monkeypatch, tmp_path):
    monitor = job_lifecycle.LifecycleMonitor(
        [_known()[0]], state_path=tmp_path / "state.json", events_path=tmp_path / "events.jsonl"
    )
    capacities = iter([0, 3, 3])
    monkeypatch.setattr(job_lifecycle, "fetch", lambda *_a, **_k: {"JOB-1": _detail("JOB-1")})
    monkeypatch.setattr(
        job_lifecycle.schedule_batch, "fetch",
        lambda *_a, **_k: {"JOB-1": [_schedule(next(capacities))]},
    )
    assert monitor.poll(object())[0] == []
    candidates, events = monitor.poll(object())
    assert candidates[0].id == "SCH-1"
    assert [event["event"] for event in events] == ["SCHEDULE_CAPACITY_AVAILABLE"]
    _, events = monitor.poll(object())
    assert events == []


def test_v7_merges_lifecycle_candidate_first_without_changing_hold_class(monkeypatch):
    instance = object.__new__(watcher_v7.LifecycleWatcher)
    known = SimpleNamespace(id="SCH-KNOWN", raw={"scheduleId": "SCH-KNOWN", "jobId": "JOB-1"})
    public = SimpleNamespace(id="SCH-PUBLIC", raw={"scheduleId": "SCH-PUBLIC", "jobId": "JOB-2"})
    instance.lifecycle_candidates = [known]
    instance.matcher = SimpleNamespace(matches=lambda _shift: (True, "ok"))
    instance.cfg = {"schedule_preferences": {}}
    monkeypatch.setattr(watcher_v7.watcher_v6.HoldReadyWatcher, "_batch_expand", lambda *_a: [public], raising=False)
    monkeypatch.setattr(instance, "_schedule_is_acceptable", lambda _schedule: (True, "ok"))
    assert instance._batch_expand([]) == [known, public]


def test_lifecycle_epoch_rearms_existing_schedule_key():
    instance = object.__new__(watcher_v7.LifecycleWatcher)
    first = SimpleNamespace(raw={
        "lifecycleSource": "known_job", "scheduleId": "SCH-1", "lifecycleEpoch": "E1"
    })
    second = SimpleNamespace(raw={
        "lifecycleSource": "known_job", "scheduleId": "SCH-1", "lifecycleEpoch": "E2"
    })
    assert instance._candidate_key(first) != instance._candidate_key(second)


def test_unconfirmed_posted_wording_never_claims_available_or_held():
    sent = []
    alert = notifier.TelegramNotifier(enabled=False)
    alert.send_text = lambda text: sent.append(text) or True
    alert.notify_job_posted_without_capacity(job_lifecycle.KnownJob("JOB-1", "YYZ4", "Brampton, ON"))
    assert "No exact schedule with positive capacity" in sent[0]
    assert "no hold was attempted" in sent[0].lower()


def test_every_schedule_alert_state_has_an_explicit_clickable_link():
    sent = []
    alert = notifier.TelegramNotifier(enabled=False)
    alert.send_text = lambda text: sent.append(text) or True
    shift = SimpleNamespace(
        title="Warehouse Associate",
        location="Brampton, ON",
        schedule="Mon 3 PM",
        pay_rate=25.60,
        url=None,
        raw={"manualUrl": "https://hiring.amazon.ca/application/?jobId=JOB-1&scheduleId=SCH-1"},
    )

    alert.notify_shift(shift, dry_run=False)
    alert.notify_held(shift, stopped_before_submit=False)
    alert.notify_hold_attention(shift, "❌ <b>Hold failed</b>", "Unavailable")
    alert.notify_identity_verification(
        shift,
        "https://hiring.amazon.ca/application/ca/?jobId=JOB-1&scheduleId=SCH-1#/liveness-check",
    )

    assert len(sent) == 4
    assert all('<a href="https://hiring.amazon.ca/' in message for message in sent)


def test_lifecycle_status_alerts_also_have_clickable_job_url():
    sent = []
    alert = notifier.TelegramNotifier(enabled=False)
    alert.send_text = lambda text: sent.append(text) or True
    job = job_lifecycle.KnownJob("JOB-1", "YYZ4", "Brampton, ON")
    url = "https://hiring.amazon.ca/app#/jobDetail?jobId=JOB-1"
    alert.notify_job_posted_without_capacity(job, url)
    alert.notify_job_unposted(job, 10.0, url)
    assert len(sent) == 2
    assert all(f'<a href="{url}">' in message for message in sent)

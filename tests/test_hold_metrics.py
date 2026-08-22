from __future__ import annotations

import hold_metrics


def _append(path, status):
    hold_metrics.append_record(
        job_id="JOB-1",
        schedule_id="SCH-1",
        title="Warehouse",
        location="Canada",
        status=status,
        message=status,
        poll_to_dispatch_ms=10,
        total_from_poll_ms=20,
        hold_timings=[("create application clicked", 15)],
        path=path,
    )


def test_latest_after_does_not_reuse_previous_run(tmp_path):
    path = tmp_path / "hold.jsonl"
    _append(path, "old")
    before = hold_metrics.count(path)

    assert hold_metrics.latest_after(before, path) is None

    _append(path, "new")
    latest = hold_metrics.latest_after(before, path)
    assert latest is not None
    assert latest["status"] == "new"

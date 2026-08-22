import schedules


def _schedule(*, hard_match, available=None):
    return schedules.Schedule({
        "jobId": "JOB-CA-TEST",
        "scheduleId": f"SCH-{hard_match}-{available}",
        "laborDemandHardMatchCount": hard_match,
        "laborDemandAvailableCount": available,
    })


def test_schedule_query_requests_hard_match_count():
    assert "laborDemandHardMatchCount" in schedules.SCHEDULE_QUERY


def test_bookable_requires_positive_hard_match_count():
    valid = _schedule(hard_match=5, available=1)
    invalid = _schedule(hard_match=0, available=9)

    assert schedules.bookable([invalid, valid]) == [valid]


def test_bookable_keeps_positive_hard_match_even_when_available_is_zero():
    valid = _schedule(hard_match=2, available=0)

    assert schedules.bookable([valid]) == [valid]


def test_bookable_treats_missing_hard_match_as_invalid():
    missing = schedules.Schedule({
        "jobId": "JOB-CA-TEST",
        "scheduleId": "SCH-MISSING",
        "laborDemandAvailableCount": 10,
    })

    assert schedules.bookable([missing]) == []

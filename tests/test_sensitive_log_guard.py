from __future__ import annotations

import logging

import sensitive_log_guard


def test_legacy_raw_hold_post_record_is_dropped():
    guard = sensitive_log_guard._SensitiveRequestFilter()
    raw = logging.LogRecord(
        "site_selectors",
        logging.INFO,
        __file__,
        1,
        "HOLD POST: url=%s\nheaders=%s\nbody=%s",
        ("https://example.invalid", {"authorization": "secret"}, "secret-body"),
        None,
    )
    assert guard.filter(raw) is False


def test_normal_hold_timing_record_is_allowed():
    guard = sensitive_log_guard._SensitiveRequestFilter()
    safe = logging.LogRecord(
        "site_selectors",
        logging.INFO,
        __file__,
        1,
        "hold timings: %s",
        ("navigation 241ms",),
        None,
    )
    assert guard.filter(safe) is True

from __future__ import annotations

import logging
import inspect

import sensitive_log_guard
import site_selectors


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


def test_legacy_hold_path_no_longer_registers_raw_request_diagnostic():
    source = inspect.getsource(site_selectors.hold_at_application)
    assert "HOLD POST:" not in source
    assert "request.post_data" not in source
    assert "request.headers" not in source

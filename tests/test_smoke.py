"""Pytest collection shim for the smoke suite.

The historical smoke suite lives in smoke_suite.py so it can stay intact while
this module owns the few assertions that intentionally track experimental live
configuration values.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

# smoke_suite.py deliberately does not match pytest's test*.py pattern. Import
# its public tests/fixtures here so the suite is still collected exactly once.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from smoke_suite import *  # noqa: F401,F403,E402

import config as config_mod  # noqa: E402


def test_polling_defaults_are_conservative_enough_for_the_waf():
    """The shipped API cadence is deliberate but still bounded and observable.

    DOM polling remains conservative because it is a full page load. API mode
    may use the explicitly experimental sub-2s cadence, but it must stay above
    the hard floor and hot mode must remain the faster cadence.
    """
    cfg = config_mod.load_config(Path(__file__).resolve().parent.parent / "config.yaml")
    polling = cfg["polling"]

    if polling["mode"] == "dom":
        assert polling["interval_seconds"] >= 30
        assert polling["hot_interval_seconds"] >= 20
    else:
        assert polling["interval_seconds"] == pytest.approx(1.25)
        assert polling["hot_interval_seconds"] == pytest.approx(0.65)
        assert polling["interval_seconds"] >= 0.5
        assert polling["hot_interval_seconds"] >= 0.5
        assert polling["hot_interval_seconds"] <= polling["interval_seconds"]

    assert polling["render_wait_ms"] >= 1000


def test_hot_interval_floor_is_enforced():
    """Even experimental API polling has an absolute lower bound."""
    cfg = config_mod._deep_merge(
        config_mod.DEFAULTS,
        {
            "polling": {
                "mode": "api",
                "interval_seconds": 1.25,
                "hot_interval_seconds": 0.49,
            }
        },
    )
    with pytest.raises(ValueError, match="hot_interval_seconds"):
        config_mod.validate_config(cfg)


def test_experimental_api_cadence_warns_instead_of_silently_claiming_safe(caplog):
    """Sub-2s API polling is allowed, but startup must say it is experimental."""
    with caplog.at_level(logging.WARNING):
        cfg = config_mod.load_config(Path(__file__).resolve().parent.parent / "config.yaml")

    assert cfg["polling"]["mode"] == "api"
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "experimental" in messages.lower()
    assert "previously measured-clean 2s" in messages

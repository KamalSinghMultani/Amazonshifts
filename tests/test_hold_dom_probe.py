from __future__ import annotations

import inspect

import hold_dom_probe
import watcher_v5


class FakePage:
    def __init__(self):
        self.binding = None
        self.script = None

    def expose_binding(self, name, callback):
        assert name == hold_dom_probe._BINDING
        self.binding = callback

    def add_init_script(self, *, script):
        self.script = script


class Result:
    def __init__(self):
        self.timings = [("navigation committed", 250.0), ("application action ready", 3200.0)]


def test_dom_probe_uses_browser_mutation_observer_not_python_polling():
    source = hold_dom_probe._INIT_SCRIPT
    assert "MutationObserver" in source
    assert "dom create inserted" in source
    assert "dom create enabled" in source
    assert "dom integrity agree inserted" in source
    assert "dom integrity agree enabled" in source
    assert "dom next inserted" in source
    assert "dom content loaded" in source
    assert "setInterval" not in source


def test_dom_probe_pushes_milestones_and_merges_chronologically(monkeypatch):
    page = FakePage()
    probe = hold_dom_probe.HoldDomProbe(page)

    clock = iter([10.0, 10.100, 10.300, 10.900])
    monkeypatch.setattr(hold_dom_probe.time, "perf_counter", lambda: next(clock))

    probe.start()
    assert page.binding is not None
    assert "MutationObserver" in page.script

    page.binding({}, "dom document start")
    page.binding({}, "dom create inserted")
    page.binding({}, "dom create enabled")

    result = Result()
    probe.annotate(result)

    names = [name for name, _ms in result.timings]
    assert names == [
        "dom document start",
        "navigation committed",
        "dom create inserted",
        "dom create enabled",
        "application action ready",
    ]


def test_watcher_profiles_before_fast_hold_and_annotates_before_metrics():
    source = inspect.getsource(watcher_v5.PreLiveWatcher._direct_hold)
    start = source.index("probe.start()")
    hold = source.index("fast_hold.hold", start)
    annotate = source.index("probe.annotate(result)", hold)
    metrics = source.index("self._record_hold_metric", annotate)
    assert start < hold < annotate < metrics

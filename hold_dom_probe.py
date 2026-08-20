"""Zero-poll browser-side timing probe for the fast reservation path.

The probe is diagnostic only. A tiny init script runs at document start and a
MutationObserver timestamps when the application controls first enter the DOM
and when their disabled state clears. It never clicks anything and never reads
credentials/tokens. Markers are pushed back through one Playwright binding, so
profiling does not add a Python<->browser round trip every 50ms.
"""

from __future__ import annotations

import logging
import time
from typing import Any

log = logging.getLogger("watcher")

_BINDING = "__amazonHoldProbeMark"
_INSTALLED_PAGE_IDS: set[int] = set()
_ACTIVE: dict[int, "HoldDomProbe"] = {}

_INIT_SCRIPT = r"""
(() => {
  if (window.__amazonHoldDomProbeInstalled) return;
  window.__amazonHoldDomProbeInstalled = true;

  const emitted = new Set();
  const emit = (label) => {
    if (emitted.has(label)) return;
    emitted.add(label);
    try {
      window.__amazonHoldProbeMark(label);
    } catch (_) {
      // Diagnostics must never interfere with Amazon's application.
    }
  };

  const normalized = (node) => ((node && node.textContent) || "")
    .replace(/\s+/g, " ")
    .trim();

  const findButton = (wanted) => {
    const buttons = document.querySelectorAll("button");
    for (const button of buttons) {
      if (normalized(button).includes(wanted)) return button;
    }
    return null;
  };

  const isEnabled = (button) => Boolean(
    button &&
    !button.disabled &&
    button.getAttribute("aria-disabled") !== "true"
  );

  const scan = () => {
    const layout = document.querySelector("[data-test-id='layout']");
    if (layout) emit("dom layout inserted");

    const next = findButton("Next");
    if (next) emit("dom next inserted");
    if (isEnabled(next)) emit("dom next enabled");

    const create = findButton("Create Application");
    if (create) emit("dom create inserted");
    if (isEnabled(create)) emit("dom create enabled");
  };

  emit("dom document start");

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => {
      emit("dom content loaded");
      scan();
    }, {once: true});
  } else {
    emit("dom content loaded");
  }

  const arm = () => {
    if (!document.documentElement) {
      setTimeout(arm, 0);
      return;
    }
    scan();
    const observer = new MutationObserver(scan);
    observer.observe(document.documentElement, {
      subtree: true,
      childList: true,
      attributes: true,
      attributeFilter: ["disabled", "aria-disabled"]
    });
  };

  arm();
})();
"""


class HoldDomProbe:
    """Collect one reservation attempt's DOM milestones relative to dispatch."""

    def __init__(self, page: Any) -> None:
        self.page = page
        self.page_id = id(page)
        self.started_at: float | None = None
        self._marks: list[tuple[str, float]] = []
        self._seen: set[str] = set()

    def _record(self, label: str) -> None:
        if self.started_at is None or label in self._seen:
            return
        self._seen.add(label)
        self._marks.append((label, (time.perf_counter() - self.started_at) * 1000))

    def install(self) -> None:
        """Install the binding/init script once for this Playwright Page."""
        if self.page_id in _INSTALLED_PAGE_IDS:
            return

        def on_mark(_source, label="") -> None:
            active = _ACTIVE.get(self.page_id)
            if active is not None and isinstance(label, str):
                active._record(label)

        try:
            self.page.expose_binding(_BINDING, on_mark)
            self.page.add_init_script(script=_INIT_SCRIPT)
            _INSTALLED_PAGE_IDS.add(self.page_id)
        except Exception as exc:  # noqa: BLE001 - profiling must never break a hold
            log.debug("could not install hold DOM probe: %s", exc)

    def start(self) -> None:
        self.install()
        self.started_at = time.perf_counter()
        self._marks.clear()
        self._seen.clear()
        _ACTIVE[self.page_id] = self

    def annotate(self, result: Any) -> None:
        """Merge browser-side milestones into HoldResult.timings chronologically."""
        if _ACTIVE.get(self.page_id) is self:
            _ACTIVE.pop(self.page_id, None)
        if not self._marks or result is None:
            return
        try:
            existing = list(getattr(result, "timings", ()) or ())
            result.timings = sorted([*existing, *self._marks], key=lambda item: item[1])
        except Exception as exc:  # noqa: BLE001
            log.debug("could not attach hold DOM timings: %s", exc)

    def stop(self) -> None:
        if _ACTIVE.get(self.page_id) is self:
            _ACTIVE.pop(self.page_id, None)

"""Defense-in-depth logging guard for known legacy request diagnostics.

Older compatibility code contains a temporary `HOLD POST:` diagnostic that
formats complete request headers/body. Those values may contain session/auth
material and must never reach console or file logs. The optimized watcher does
not rely on that diagnostic, so drop the entire record at the logging boundary.

This is intentionally narrow: normal URLs, timing, status, and sanitized error
messages continue to log unchanged.
"""

from __future__ import annotations

import logging


class _SensitiveRequestFilter(logging.Filter):
    BLOCKED_PREFIXES = (
        "HOLD POST:",
    )

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message_template = str(record.msg or "")
        except Exception:
            return True
        return not message_template.startswith(self.BLOCKED_PREFIXES)


_FILTER = _SensitiveRequestFilter()
_INSTALLED = False


def install() -> None:
    """Install once on the legacy selector logger that owns the raw diagnostic."""
    global _INSTALLED
    if _INSTALLED:
        return
    logging.getLogger("site_selectors").addFilter(_FILTER)
    _INSTALLED = True

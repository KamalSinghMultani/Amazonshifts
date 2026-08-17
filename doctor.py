"""`python watcher.py --doctor` — is this environment actually ready?

The motivating problem: this site has two independent sessions. Job search is
public, so a signed-out watcher polls, matches, ranks and alerts exactly like a
healthy one, and the first symptom of a missing login is a hold failing on a
shift that was really there. That is not something to discover at 6am.

Every check here works with no job postings available, which matters because
hiring.amazon.ca is empty most of the time.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import site_selectors

log = logging.getLogger(__name__)

OK, WARN, FAIL = "ok", "warn", "fail"

MARKS = {OK: "[ ok ]", WARN: "[warn]", FAIL: "[FAIL]"}


class Check:
    def __init__(self, name: str, state: str, detail: str = "", fix: str = "") -> None:
        self.name = name
        self.state = state
        self.detail = detail
        self.fix = fix

    def render(self) -> str:
        line = f"{MARKS[self.state]} {self.name}"
        if self.detail:
            line += f" — {self.detail}"
        return line


def verdict(checks: list[Check]) -> tuple[int, str]:
    """Exit code and a one-line summary.

    A warning is not a failure: alerting still works without a portal login,
    and saying so precisely is the whole point of this command.
    """
    if any(c.state == FAIL for c in checks):
        return 2, "NOT READY — detection itself is broken."
    if any(c.state == WARN for c in checks):
        return 1, "PARTLY READY — it will detect and alert, but cannot hold a shift."
    return 0, "READY — detect, alert and hold are all good to go."


def check_selectors() -> list[Check]:
    checks = []
    missing_detection = site_selectors.unconfigured_detection()
    checks.append(Check(
        "detection selectors",
        FAIL if missing_detection else OK,
        ", ".join(missing_detection) if missing_detection else "all configured",
        fix="fill them in in site_selectors.py",
    ))
    missing_hold = site_selectors.unconfigured_hold()
    checks.append(Check(
        "hold selectors",
        WARN if missing_hold else OK,
        ", ".join(missing_hold) if missing_hold else "all configured",
        fix="fill them in in site_selectors.py",
    ))
    return checks


def check_portal_login(page: Any, base_url: str, settle_ms: int = 7000) -> Check:
    """Is the *hiring portal* signed in — not merely the public site?

    Confirmed experimentally against a signed-in and a signed-out profile:
    /application/ stays put when signed in, and bounces to
    auth.hiring.amazon.com when signed out. It needs no job posting, which is
    what makes it usable while Canada is empty.
    """
    url = base_url.rstrip("/") + "/application/"
    try:
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(settle_ms)
    except Exception as exc:  # noqa: BLE001
        return Check("hiring portal login", WARN, f"could not open {url}: {exc}",
                     fix="python save_session.py")

    if site_selectors.is_login_page(page):
        return Check(
            "hiring portal login", WARN,
            "signed OUT — alerts will work, holding will not",
            fix="python save_session.py   (then open a job and press "
                "'Select schedule' before pressing Enter)",
        )
    return Check("hiring portal login", OK, "signed in")


def check_job_search(page: Any, job_search_url: str, settle_ms: int = 6000) -> Check:
    try:
        page.goto(job_search_url, wait_until="domcontentloaded")
        page.wait_for_timeout(settle_ms)
    except Exception as exc:  # noqa: BLE001
        return Check("job search page", FAIL, str(exc)[:120])

    state, detail = site_selectors.page_state(page)
    if state == "ok":
        return Check("job search page", OK, "loads normally")
    if state == "stale":
        return Check("job search page", WARN, f"token expired ({detail}) — a reload fixes it")
    return Check("job search page", FAIL, f"{state}: {detail}")


def check_api(client: Any, token_source: Any) -> list[Check]:
    checks = []
    token = token_source.current() if token_source else None
    checks.append(Check(
        "api auth token",
        OK if token else WARN,
        f"captured ({len(token)} chars)" if token else "none captured yet",
        fix="the first poll refreshes it on a 401",
    ))

    started = time.perf_counter()
    try:
        shifts = client.fetch_shifts()
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("api poll", FAIL, str(exc)[:150]))
        return checks

    elapsed = (time.perf_counter() - started) * 1000
    # Zero is a perfectly healthy answer here — Canada is empty most of the
    # time — so it is reported, not judged.
    detail = f"{len(shifts)} job(s) in {elapsed:.0f}ms"
    if shifts:
        # Name them. Postings are rare and last about a minute, so a bare
        # count leaves you unable to tell afterwards whether the one you saw
        # was even in your area.
        detail += " — " + "; ".join(s.summary() for s in shifts[:3])
        if len(shifts) > 3:
            detail += f"; +{len(shifts) - 3} more"
    checks.append(Check("api poll", OK, detail))
    return checks


def render(checks: list[Check], title: str) -> str:
    lines = [f"\n{title}", "=" * len(title)]
    lines += [c.render() for c in checks]
    code, summary = verdict(checks)
    lines += ["", summary]
    fixes = [c.fix for c in checks if c.state != OK and c.fix]
    if fixes:
        lines += ["", "Next:"]
        lines += [f"  - {fix}" for fix in dict.fromkeys(fixes)]
    return "\n".join(lines)

"""Non-destructive proof that the Hiring application session is usable.

This module deliberately does not create an application or reserve anything.
It proves the strongest things we can verify without a live shift:

1. the authenticated page is on the configured country host;
2. the login state detector has positive authenticated evidence (URL alone is
   not accepted);
3. the same fresh session can open the application shell without being bounced
   back to the auth domain.

A real reservation can only be proven when a real schedule exists, but this
closes the gap where the public job API worked while the application session
was actually signed out.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from urllib.parse import urlparse

import relogin as login_flow
import site_selectors


@dataclass(frozen=True)
class SessionProof:
    passed: bool
    expected_host: str
    authenticated_host: str
    authenticated_state: str
    application_host: str
    application_redirected_to_login: bool
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)

    def summary(self) -> str:
        return (
            f"fresh_auth={self.authenticated_state}@{self.authenticated_host or '<none>'}; "
            f"application={self.application_host or '<none>'}; "
            f"redirected_to_login={self.application_redirected_to_login}; "
            f"passed={self.passed}"
        )


def _host(url: str) -> str:
    return (urlparse(url or "").hostname or "").lower()


def _failed(
    *,
    expected_host: str,
    authenticated_host: str,
    authenticated_state: str,
    application_host: str = "",
    redirected: bool = False,
    reason: str,
) -> SessionProof:
    return SessionProof(
        passed=False,
        expected_host=expected_host,
        authenticated_host=authenticated_host,
        authenticated_state=authenticated_state,
        application_host=application_host,
        application_redirected_to_login=redirected,
        reason=reason,
    )


def prove_fresh_session(page, base_url: str, *, settle_ms: int = 1500) -> SessionProof:
    """Verify a just-authenticated session without creating an application."""
    expected_host = _host(base_url)
    authenticated_host = _host(getattr(page, "url", ""))
    detector = login_flow.StateDetector(page)
    state = detector.detect_state()
    state_name = getattr(state, "name", str(state))

    if authenticated_host != expected_host:
        return _failed(
            expected_host=expected_host,
            authenticated_host=authenticated_host,
            authenticated_state=state_name,
            reason=(
                f"fresh authentication landed on {authenticated_host or '<none>'}, "
                f"expected {expected_host}"
            ),
        )

    if state != login_flow.AuthState.AUTHENTICATED:
        return _failed(
            expected_host=expected_host,
            authenticated_host=authenticated_host,
            authenticated_state=state_name,
            reason="fresh login did not have positive authenticated UI evidence",
        )

    application_url = base_url.rstrip("/") + "/application/"
    try:
        page.goto(application_url, wait_until="domcontentloaded")
        page.wait_for_timeout(settle_ms)
    except Exception as exc:  # noqa: BLE001
        return _failed(
            expected_host=expected_host,
            authenticated_host=authenticated_host,
            authenticated_state=state_name,
            reason=f"could not open application shell: {str(exc)[:200]}",
        )

    application_host = _host(getattr(page, "url", ""))
    redirected = (
        "auth.hiring.amazon" in application_host
        or site_selectors.is_login_page(page)
    )

    if application_host != expected_host:
        return _failed(
            expected_host=expected_host,
            authenticated_host=authenticated_host,
            authenticated_state=state_name,
            application_host=application_host,
            redirected=redirected,
            reason=(
                f"application shell landed on {application_host or '<none>'}, "
                f"expected {expected_host}"
            ),
        )

    if redirected:
        return _failed(
            expected_host=expected_host,
            authenticated_host=authenticated_host,
            authenticated_state=state_name,
            application_host=application_host,
            redirected=True,
            reason="application shell redirected to login",
        )

    return SessionProof(
        passed=True,
        expected_host=expected_host,
        authenticated_host=authenticated_host,
        authenticated_state=state_name,
        application_host=application_host,
        application_redirected_to_login=False,
        reason="fresh country-specific authentication and application-shell access verified",
    )


def prove_existing_session(page, base_url: str, *, settle_ms: int = 1500) -> SessionProof:
    """Strong health check for an existing session.

    We first load the country job-search page and require positive account UI
    evidence. This avoids treating the public URL itself as proof. The second
    stage reuses prove_fresh_session() to verify application-shell access.
    """
    expected_host = _host(base_url)
    search_url = base_url.rstrip("/") + "/app#/jobSearch"
    try:
        page.goto(search_url, wait_until="domcontentloaded")
        page.wait_for_timeout(settle_ms)
    except Exception as exc:  # noqa: BLE001
        return _failed(
            expected_host=expected_host,
            authenticated_host=_host(getattr(page, "url", "")),
            authenticated_state="UNKNOWN",
            reason=f"could not open country job-search page: {str(exc)[:200]}",
        )

    return prove_fresh_session(page, base_url, settle_ms=settle_ms)

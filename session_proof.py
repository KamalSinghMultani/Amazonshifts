"""Non-destructive proof that the Hiring application session is usable.

This module deliberately does not create an application or reserve anything.
It proves the strongest things we can verify without a live shift:

1. the authenticated page is on the configured country host;
2. the login state detector has positive authenticated evidence (URL alone is
   not accepted);
3. a saved session can open a protected country-specific application route
   without being bounced back to the auth domain.

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


def _application_probe_url(base_url: str) -> str:
    """Country-specific protected route used only to prove a saved session.

    Live Canadian auth returns to /application/ca/#/consent. Using that route
    avoids the ambiguous bare /application/ landing which can sit on an empty
    #/pre-consent shell even after authentication succeeded.
    """
    host = _host(base_url)
    if host.endswith("amazon.ca"):
        country = "ca"
    elif host.endswith("amazon.com"):
        country = "us"
    else:
        return base_url.rstrip("/") + "/application/"
    return base_url.rstrip("/") + f"/application/{country}/#/consent"


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


def _prove_current_application_page(page, base_url: str, *, reason: str) -> SessionProof:
    """Prove the currently loaded protected application page."""
    expected_host = _host(base_url)
    current_host = _host(getattr(page, "url", ""))
    redirected = (
        "auth.hiring.amazon" in current_host
        or site_selectors.is_login_page(page)
    )
    detector = login_flow.StateDetector(page)
    state = detector.detect_state()
    state_name = getattr(state, "name", str(state))

    if current_host != expected_host:
        return _failed(
            expected_host=expected_host,
            authenticated_host=current_host,
            authenticated_state=state_name,
            application_host=current_host,
            redirected=redirected,
            reason=(
                f"application shell landed on {current_host or '<none>'}, "
                f"expected {expected_host}"
            ),
        )

    if redirected:
        return _failed(
            expected_host=expected_host,
            authenticated_host=current_host,
            authenticated_state=state_name,
            application_host=current_host,
            redirected=True,
            reason="application shell redirected to login",
        )

    if state != login_flow.AuthState.AUTHENTICATED:
        return _failed(
            expected_host=expected_host,
            authenticated_host=current_host,
            authenticated_state=state_name,
            application_host=current_host,
            redirected=False,
            reason="application shell lacked positive authenticated evidence",
        )

    return SessionProof(
        passed=True,
        expected_host=expected_host,
        authenticated_host=current_host,
        authenticated_state=state_name,
        application_host=current_host,
        application_redirected_to_login=False,
        reason=reason,
    )


def _wait_for_application_proof(
    page,
    base_url: str,
    *,
    reason: str,
    timeout_ms: int = 6000,
) -> SessionProof:
    """Return as soon as the protected app proves auth or definitely redirects.

    React can mount after DOMContentLoaded. Polling strong evidence avoids both a
    fixed multi-second sleep and the false negative seen when #/pre-consent was
    inspected before any application elements had mounted.
    """
    waited = 0
    last = _prove_current_application_page(page, base_url, reason=reason)
    while not last.passed and waited < timeout_ms:
        if last.application_redirected_to_login:
            return last
        if last.application_host and last.application_host != _host(base_url):
            return last
        try:
            page.wait_for_timeout(250)
        except Exception:
            return last
        waited += 250
        last = _prove_current_application_page(page, base_url, reason=reason)
    return last


def prove_fresh_session(page, base_url: str, *, settle_ms: int = 1500) -> SessionProof:
    """Verify a just-authenticated session without creating an application.

    Crucially, if the auth state machine already landed on a protected
    application page, do not navigate away from that proven page. The old proof
    navigated to bare /application/, which changed a good /#/consent landing into
    an unmounted /#/pre-consent shell and discarded the recovered session.
    """
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

    current_url = (getattr(page, "url", "") or "").lower()
    if "/application/" in current_url:
        return SessionProof(
            passed=True,
            expected_host=expected_host,
            authenticated_host=authenticated_host,
            authenticated_state=state_name,
            application_host=authenticated_host,
            application_redirected_to_login=False,
            reason="fresh country-specific authentication verified on protected application page",
        )

    application_url = _application_probe_url(base_url)
    try:
        page.goto(application_url, wait_until="domcontentloaded")
    except Exception as exc:  # noqa: BLE001
        return _failed(
            expected_host=expected_host,
            authenticated_host=authenticated_host,
            authenticated_state=state_name,
            reason=f"could not open protected application route: {str(exc)[:200]}",
        )

    proof = _wait_for_application_proof(
        page,
        base_url,
        reason="fresh country-specific authentication and application access verified",
        timeout_ms=max(6000, settle_ms),
    )
    if proof.passed:
        return SessionProof(
            passed=True,
            expected_host=proof.expected_host,
            authenticated_host=authenticated_host,
            authenticated_state=state_name,
            application_host=proof.application_host,
            application_redirected_to_login=False,
            reason=proof.reason,
        )
    return proof


def prove_existing_session(page, base_url: str, *, settle_ms: int = 1500) -> SessionProof:
    """Strong health check for an existing saved session.

    Do not use the public job-search page as auth proof and do not use the bare
    /application/ landing. Navigate directly to the country-specific protected
    consent route and return as soon as strict application evidence appears.
    An expired session should instead redirect to the auth host and fail.
    """
    expected_host = _host(base_url)
    application_url = _application_probe_url(base_url)

    try:
        page.goto(application_url, wait_until="domcontentloaded")
    except Exception as exc:  # noqa: BLE001
        return _failed(
            expected_host=expected_host,
            authenticated_host=_host(getattr(page, "url", "")),
            authenticated_state="UNKNOWN",
            application_host=_host(getattr(page, "url", "")),
            reason=f"could not open protected application route: {str(exc)[:200]}",
        )

    return _wait_for_application_proof(
        page,
        base_url,
        reason="saved country-specific application session strongly verified",
        timeout_ms=max(6000, settle_ms),
    )

"""Non-destructive proof that the Hiring application session is usable.

This module deliberately does not create an application or reserve anything.
It proves the strongest things we can verify without a live shift:

1. the authenticated page is on the configured country host;
2. the login state detector has positive authenticated evidence (URL alone is
   not accepted);
3. the protected application shell is reachable without an auth redirect; and
4. the shell's own harmless authenticated candidate read succeeds.

The backend read is observed passively from the page's normal network traffic.
No authorization/cookie/token values are read or logged. A real reservation can
only be proven when a real schedule exists, but this prevents stale UI/storage
from falsely re-arming holding after the application session has expired.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from urllib.parse import urlparse

import relogin as login_flow
import site_selectors


_PROTECTED_CANDIDATE_PATH = "/application/api/candidate-application/candidate"


@dataclass(frozen=True)
class SessionProof:
    passed: bool
    expected_host: str
    authenticated_host: str
    authenticated_state: str
    application_host: str
    application_redirected_to_login: bool
    application_backend_authenticated: bool
    application_backend_unauthorized: bool
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)

    def summary(self) -> str:
        return (
            f"fresh_auth={self.authenticated_state}@{self.authenticated_host or '<none>'}; "
            f"application={self.application_host or '<none>'}; "
            f"redirected_to_login={self.application_redirected_to_login}; "
            f"backend_authenticated={self.application_backend_authenticated}; "
            f"backend_unauthorized={self.application_backend_unauthorized}; "
            f"passed={self.passed}"
        )


class _ProtectedBackendProbe:
    """Observe the app's own candidate GET without inspecting headers or body."""

    def __init__(self, expected_host: str) -> None:
        self.expected_host = expected_host
        self.authenticated = False
        self.unauthorized = False
        self.seen = False

    def observe(self, response) -> None:
        try:
            url = str(getattr(response, "url", "") or "")
            parsed = urlparse(url)
            if (parsed.hostname or "").lower() != self.expected_host:
                return
            if parsed.path.rstrip("/").lower() != _PROTECTED_CANDIDATE_PATH:
                return

            request = getattr(response, "request", None)
            method = str(getattr(request, "method", "GET") or "GET").upper()
            if method != "GET":
                return

            status = int(getattr(response, "status", 0) or 0)
            self.seen = True
            if 200 <= status < 300:
                self.authenticated = True
                self.unauthorized = False
            elif status == 401:
                # A 401 from this same-origin protected candidate read is strong
                # evidence that the application auth is no longer accepted.
                self.unauthorized = True
                self.authenticated = False
            # 403 is deliberately NOT treated as definitive expiry because the
            # site is WAF-fronted and a block must remain inconclusive.
        except Exception:
            # Proof is fail-closed. If observation itself fails, no backend
            # success is recorded and the proof cannot re-arm holding.
            return

    def attach(self, page) -> None:
        try:
            page.on("response", self.observe)
        except Exception:
            # A page that cannot expose response events cannot produce the
            # required protected-backend proof.
            return


def _host(url: str) -> str:
    return (urlparse(url or "").hostname or "").lower()


def _application_probe_url(base_url: str) -> str:
    """Country-specific protected route used only to prove a saved session."""
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
    backend_authenticated: bool = False,
    backend_unauthorized: bool = False,
    reason: str,
) -> SessionProof:
    return SessionProof(
        passed=False,
        expected_host=expected_host,
        authenticated_host=authenticated_host,
        authenticated_state=authenticated_state,
        application_host=application_host,
        application_redirected_to_login=redirected,
        application_backend_authenticated=backend_authenticated,
        application_backend_unauthorized=backend_unauthorized,
        reason=reason,
    )


def _prove_current_application_page(page, base_url: str, *, reason: str) -> SessionProof:
    """Collect the protected application's UI/auth state.

    This is necessary evidence but is no longer sufficient by itself. Stale
    application DOM plus stale localStorage token *keys* survived a real logout
    during live testing and produced false RESTORED messages. Final success is
    awarded only by _wait_for_application_proof after a protected backend read.
    """
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

    # UI-only success is deliberately provisional. The caller must pair it with
    # a successful protected backend read before returning passed=True.
    return SessionProof(
        passed=True,
        expected_host=expected_host,
        authenticated_host=current_host,
        authenticated_state=state_name,
        application_host=current_host,
        application_redirected_to_login=False,
        application_backend_authenticated=False,
        application_backend_unauthorized=False,
        reason=reason,
    )


def _wait_for_application_proof(
    page,
    base_url: str,
    *,
    reason: str,
    backend_probe: _ProtectedBackendProbe,
    timeout_ms: int = 6000,
) -> SessionProof:
    """Require both protected UI evidence and a successful candidate API read."""
    waited = 0
    last = _prove_current_application_page(page, base_url, reason=reason)

    while True:
        if last.application_redirected_to_login:
            return last
        if last.application_host and last.application_host != _host(base_url):
            return last

        if backend_probe.unauthorized:
            return _failed(
                expected_host=_host(base_url),
                authenticated_host=last.authenticated_host,
                authenticated_state=last.authenticated_state,
                application_host=last.application_host,
                backend_unauthorized=True,
                reason="protected candidate read returned 401; application session is not authenticated",
            )

        if last.passed and backend_probe.authenticated:
            return SessionProof(
                passed=True,
                expected_host=last.expected_host,
                authenticated_host=last.authenticated_host,
                authenticated_state=last.authenticated_state,
                application_host=last.application_host,
                application_redirected_to_login=False,
                application_backend_authenticated=True,
                application_backend_unauthorized=False,
                reason=reason + "; protected candidate read returned 2xx",
            )

        if waited >= timeout_ms:
            break

        try:
            page.wait_for_timeout(250)
        except Exception:
            break
        waited += 250
        last = _prove_current_application_page(page, base_url, reason=reason)

    if last.passed:
        return _failed(
            expected_host=last.expected_host,
            authenticated_host=last.authenticated_host,
            authenticated_state=last.authenticated_state,
            application_host=last.application_host,
            reason=(
                "protected application UI looked authenticated but no successful "
                "protected candidate read was observed"
            ),
        )
    return last


def _probe_application(
    page,
    base_url: str,
    target_url: str,
    *,
    reason: str,
    timeout_ms: int,
    force_reload: bool = False,
) -> SessionProof:
    """Navigate harmlessly while observing the shell's protected candidate GET.

    Fresh authentication can finish on the exact consent URL we need to probe.
    Calling ``goto`` with that same SPA URL is not guaranteed to rebuild the
    application shell, so no candidate request may be emitted even when login
    succeeded. For a fresh-login proof we therefore force a real browser reload
    after attaching the response observer. Existing-session checks still use a
    normal navigation from their blank worker page.
    """
    expected_host = _host(base_url)
    backend_probe = _ProtectedBackendProbe(expected_host)
    backend_probe.attach(page)

    try:
        current_url = str(getattr(page, "url", "") or "")
        if force_reload and current_url == target_url and hasattr(page, "reload"):
            page.reload(wait_until="domcontentloaded")
        else:
            page.goto(target_url, wait_until="domcontentloaded")
    except Exception as exc:  # noqa: BLE001
        # A reload may fail during a just-finished auth navigation. One ordinary
        # navigation is a safe fallback and still cannot create an application.
        if force_reload:
            try:
                page.goto(target_url, wait_until="domcontentloaded")
            except Exception as fallback_exc:  # noqa: BLE001
                return _failed(
                    expected_host=expected_host,
                    authenticated_host=_host(getattr(page, "url", "")),
                    authenticated_state="UNKNOWN",
                    application_host=_host(getattr(page, "url", "")),
                    reason=(
                        "could not re-open protected application route after login: "
                        f"{str(fallback_exc)[:200]}"
                    ),
                )
        else:
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
        reason=reason,
        backend_probe=backend_probe,
        timeout_ms=timeout_ms,
    )


def prove_fresh_session(page, base_url: str, *, settle_ms: int = 1500) -> SessionProof:
    """Verify a just-authenticated session without creating an application.

    A fresh login's UI state is checked first, then the same protected page is
    force-reloaded (or the country-specific consent route is opened) so the
    application really boots and emits its protected candidate GET. Reusing the
    country-specific route avoids the old bad behavior of navigating a good
    landing to ambiguous bare /application/.
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

    current_url = str(getattr(page, "url", "") or "")
    target_url = current_url if "/application/" in current_url.lower() else _application_probe_url(base_url)
    proof = _probe_application(
        page,
        base_url,
        target_url,
        reason="fresh country-specific authentication and application backend access verified",
        timeout_ms=max(6000, settle_ms),
        force_reload=True,
    )
    if proof.passed:
        return SessionProof(
            passed=True,
            expected_host=proof.expected_host,
            authenticated_host=authenticated_host,
            authenticated_state=state_name,
            application_host=proof.application_host,
            application_redirected_to_login=False,
            application_backend_authenticated=True,
            application_backend_unauthorized=False,
            reason=proof.reason,
        )
    return proof


def prove_existing_session(page, base_url: str, *, settle_ms: int = 1500) -> SessionProof:
    """Strong health check for an existing saved session.

    The page must both render positive protected-application evidence and make
    its own authenticated candidate GET successfully. A stale consent shell or
    stale localStorage token key names can no longer produce a green proof.
    """
    return _probe_application(
        page,
        base_url,
        _application_probe_url(base_url),
        reason="saved country-specific application session strongly verified",
        timeout_ms=max(6000, settle_ms),
    )

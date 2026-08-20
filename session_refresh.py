"""Refresh/prove the Amazon Hiring session in a separate process.

The main watcher keeps polling while this helper proves an existing application
session or, when that proof fails, runs the project's existing authentication
state machine. This module does not implement a second login/CAPTCHA stack: it
reuses relogin.py and only adds strict proof, safe failure diagnostics, and a
storage-state handoff back to the live watcher.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

import auth_evidence
import browser_launch
import failure_capture
import relogin as login_flow
import relogin_patch
import session_proof
from config import load_config, load_dotenv

# Keep the standalone helper on the same strict auth semantics as the watcher.
relogin_patch.apply_patch(login_flow)


def _write(path: Path, **payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), "utf-8")


def _host(url: str) -> str:
    return (urlparse(url or "").hostname or "").lower()


def _diagnose_auth_failure(page, manager, returned_state, base_url: str = "") -> dict:
    """Describe why authentication stopped without exposing credentials/tokens."""
    machine_state = getattr(getattr(manager, "auth_machine", None), "state", None)
    machine_name = getattr(machine_state, "name", str(machine_state or "UNKNOWN"))
    returned_name = getattr(returned_state, "name", str(returned_state or "UNKNOWN"))
    challenge_type = "NONE"
    final_host = _host(getattr(page, "url", ""))
    expected_host = _host(base_url)

    try:
        detector = manager.auth_machine.detector
        detected = detector.detect_captcha_type()
        challenge_type = getattr(detected, "name", str(detected))
    except Exception:
        pass

    if returned_state == login_flow.AuthState.BAD_CREDENTIALS:
        category = "bad_credentials"
    elif returned_state == login_flow.AuthState.OTP_TIMEOUT:
        category = "otp_timeout"
    elif machine_name in ("CAPTCHA_REQUIRED", "CAPTCHA_FAILED") or challenge_type != "NONE":
        category = "challenge_not_cleared"
    elif (
        returned_state == login_flow.AuthState.SESSION_ERROR
        and machine_name == "UNKNOWN_PAGE"
        and challenge_type == "NONE"
        and expected_host
        and final_host == expected_host
    ):
        category = "post_auth_page_unrecognized"
    elif returned_state == login_flow.AuthState.SESSION_ERROR:
        category = "state_machine_error"
    else:
        category = "authentication_incomplete"

    return {
        "category": category,
        "returned_state": returned_name,
        "machine_state": machine_name,
        "challenge_type": challenge_type,
        "final_host": final_host,
    }


def _late_auth_recheck(page, manager, state, base_url: str, *, timeout_seconds: float = 4.0):
    """Catch a protected app page that finishes mounting just after auth timeout."""
    if state == login_flow.AuthState.AUTHENTICATED:
        return state
    if state != login_flow.AuthState.SESSION_ERROR:
        return state
    if _host(getattr(page, "url", "")) != _host(base_url):
        return state

    detector = manager.auth_machine.detector
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while True:
        try:
            observed = detector.detect_state()
        except Exception:
            return state

        if observed == login_flow.AuthState.AUTHENTICATED:
            manager.auth_machine.state = observed
            return observed

        if time.monotonic() >= deadline:
            return state

        try:
            page.wait_for_timeout(250)
        except Exception:
            time.sleep(0.25)


def _forced_login(page, base_url: str) -> tuple[str, str, dict]:
    """Run the existing auth state machine even if an old page looks healthy."""
    if login_flow.credentials() is None:
        return (
            login_flow.UNKNOWN,
            "no credentials in .env",
            {"category": "credentials_missing", "final_host": _host(page.url)},
        )

    manager = login_flow.create_auth_system(page, use_mock_solver=False)
    try:
        state = manager.auth_machine.run(base_url)
    except Exception as exc:  # noqa: BLE001
        return (
            login_flow.UNKNOWN,
            f"authentication state machine raised: {str(exc)[:200]}",
            {
                "category": "state_machine_exception",
                "final_host": _host(getattr(page, "url", "")),
            },
        )

    state = _late_auth_recheck(page, manager, state, base_url)
    diagnostics = _diagnose_auth_failure(page, manager, state, base_url)

    if state == login_flow.AuthState.AUTHENTICATED:
        return login_flow.OK, "fresh session established", diagnostics
    if state == login_flow.AuthState.BAD_CREDENTIALS:
        return login_flow.BAD_CREDENTIALS, "the email or PIN was rejected", diagnostics
    if state in (login_flow.AuthState.CAPTCHA_REQUIRED, login_flow.AuthState.CAPTCHA_FAILED):
        return login_flow.CAPTCHA, "a challenge remained after the configured solver ran", diagnostics
    if state == login_flow.AuthState.OTP_TIMEOUT:
        return login_flow.OTP_REQUIRED, "the code was requested but never arrived", diagnostics
    if diagnostics.get("category") == "challenge_not_cleared":
        return login_flow.CAPTCHA, "challenge solving did not produce an authenticated transition", diagnostics
    if diagnostics.get("category") == "post_auth_page_unrecognized":
        return (
            login_flow.UNKNOWN,
            "challenge is no longer present and the flow returned to the configured country site, "
            "but the final page lacks a known positive authenticated marker",
            diagnostics,
        )
    return login_flow.UNKNOWN, f"authentication failed at state: {state.name}", diagnostics


def _persist_success(
    context,
    output_state: Path,
    result_path: Path,
    status: str,
    proof,
    *,
    precheck=None,
) -> int:
    output_state.parent.mkdir(parents=True, exist_ok=True)
    context.storage_state(path=str(output_state))
    payload = {
        "status": status,
        "detail": proof.reason + "; " + proof.summary(),
        "proof": proof.to_dict(),
    }
    if precheck is not None:
        payload["precheck"] = precheck.to_dict()
    _write(result_path, **payload)
    return 0


def _capture_failure(page, context, base_url: str, category: str, *, detail: str = "") -> dict:
    """Best-effort screenshot/sidecar for unattended auth failures."""
    try:
        return failure_capture.capture(
            page,
            context,
            base_url,
            f"session-{category}",
            extra={"detail": str(detail or "")[:300]},
        )
    except Exception as exc:  # noqa: BLE001
        return {"capture_error": str(exc)[:240], "screenshot": "", "sidecar": ""}


def _with_capture(detail: str, captured: dict) -> str:
    shot = str(captured.get("screenshot") or "")
    return f"{detail}; failure screenshot: {shot}" if shot else detail


def run(config_path: str, output_state: Path, result_path: Path, force_login: bool) -> int:
    load_dotenv()
    cfg = load_config(config_path)
    storage = Path(cfg["browser"]["storage_state"])
    base_url = cfg["site"]["base_url"]

    browser_cfg = dict(cfg["browser"])
    browser_cfg["user_data_dir"] = None
    browser_cfg["headless"] = True

    try:
        with sync_playwright() as playwright:
            browser, context = browser_launch.launch_context(
                playwright,
                browser_cfg,
                storage_state=str(storage) if storage.exists() else None,
            )
            context.set_default_timeout(browser_cfg["action_timeout_ms"])
            context.set_default_navigation_timeout(browser_cfg["nav_timeout_ms"])
            page = context.pages[0] if context.pages else context.new_page()

            precheck = None
            if not force_login:
                precheck = session_proof.prove_existing_session(page, base_url, settle_ms=1500)
                if precheck.passed:
                    rc = _persist_success(
                        context, output_state, result_path, "healthy", precheck
                    )
                    browser_launch.close_context(browser, context)
                    return rc

            status, detail, diagnostics = _forced_login(page, base_url)

            if status == login_flow.OK:
                proof = session_proof.prove_fresh_session(page, base_url, settle_ms=1500)
                if proof.passed:
                    rc = _persist_success(
                        context,
                        output_state,
                        result_path,
                        "ok",
                        proof,
                        precheck=precheck,
                    )
                    browser_launch.close_context(browser, context)
                    return rc

                evidence = auth_evidence.collect(page, context, base_url)
                failure_detail = (
                    f"authentication reported success but session proof failed: {proof.reason}"
                )
                captured = _capture_failure(
                    page, context, base_url, "proof-failed", detail=failure_detail
                )
                _write(
                    result_path,
                    status="proof_failed",
                    detail=_with_capture(failure_detail, captured),
                    proof=proof.to_dict(),
                    precheck=precheck.to_dict() if precheck is not None else None,
                    auth_diagnostics=diagnostics,
                    auth_evidence=evidence,
                    failure_capture=captured,
                )
                browser_launch.close_context(browser, context)
                return 2

            evidence = auth_evidence.collect(page, context, base_url)
            category = str(diagnostics.get("category") or status or "unknown")
            captured = _capture_failure(page, context, base_url, category, detail=detail)
            _write(
                result_path,
                status=status,
                detail=_with_capture(detail, captured),
                precheck=precheck.to_dict() if precheck is not None else None,
                auth_diagnostics=diagnostics,
                auth_evidence=evidence,
                failure_capture=captured,
            )
            browser_launch.close_context(browser, context)
            return 2
    except Exception as exc:  # noqa: BLE001
        _write(result_path, status="error", detail=str(exc)[:500])
        return 3


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--output-state", required=True)
    parser.add_argument("--result", required=True)
    parser.add_argument("--force-login", action="store_true")
    args = parser.parse_args(argv)
    return run(
        args.config,
        Path(args.output_state),
        Path(args.result),
        args.force_login,
    )


if __name__ == "__main__":
    raise SystemExit(main())

"""Refresh the Amazon Hiring session in a separate process.

The main watcher must keep polling while authentication does slow work (page
loads, OTP, or a challenge). This helper owns its own Playwright instance and
writes a fresh storage-state file on success. The watcher then imports the new
cookies into its already-running context and reloads the token page.

It deliberately uses a temporary non-persistent browser context so it never
fights the watcher's live Chrome profile lock.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from playwright.sync_api import sync_playwright

import browser_launch
import doctor
import relogin as login_flow
from config import load_config, load_dotenv


def _write(path: Path, **payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), "utf-8")


def _forced_login(page, base_url: str) -> tuple[str, str]:
    """Run the auth state machine even if the existing page looks signed in.

    login_flow.attempt() first asks whether the current page appears
    authenticated. That is correct for expiry recovery, but a proactive
    100-minute refresh must actually replace the session rather than declaring
    the old one healthy and returning immediately.
    """
    if login_flow.credentials() is None:
        return login_flow.UNKNOWN, "no credentials in .env"

    manager = login_flow.create_auth_system(page, use_mock_solver=False)
    try:
        state = manager.auth_machine.run(base_url)
    except Exception as exc:  # noqa: BLE001
        return login_flow.UNKNOWN, f"forced login raised: {str(exc)[:200]}"

    if state == login_flow.AuthState.AUTHENTICATED:
        return login_flow.OK, "fresh session established"
    if state == login_flow.AuthState.BAD_CREDENTIALS:
        return login_flow.BAD_CREDENTIALS, "the email or PIN was rejected"
    if state in (login_flow.AuthState.CAPTCHA_REQUIRED, login_flow.AuthState.CAPTCHA_FAILED):
        return login_flow.CAPTCHA, "a CAPTCHA blocked the login"
    if state == login_flow.AuthState.OTP_TIMEOUT:
        return login_flow.OTP_REQUIRED, "the code was requested but never arrived"
    return login_flow.UNKNOWN, f"authentication failed at state: {state.name}"


def run(config_path: str, output_state: Path, result_path: Path, force_login: bool) -> int:
    load_dotenv()
    cfg = load_config(config_path)
    storage = Path(cfg["browser"]["storage_state"])

    # Never reuse the live persistent profile from another process. Seed an
    # isolated context with the last saved cookies instead.
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

            # A health-only pass avoids needless login attempts. Scheduled
            # proactive refreshes intentionally bypass this shortcut.
            if not force_login:
                check = doctor.check_portal_login(
                    page, cfg["site"]["base_url"], settle_ms=1500
                )
                if check.state == doctor.OK:
                    output_state.parent.mkdir(parents=True, exist_ok=True)
                    context.storage_state(path=str(output_state))
                    _write(result_path, status="healthy", detail=check.detail)
                    browser_launch.close_context(browser, context)
                    return 0

            status, detail = (
                _forced_login(page, cfg["site"]["base_url"])
                if force_login
                else login_flow.attempt(page, cfg["site"]["base_url"])
            )
            if status == login_flow.OK:
                output_state.parent.mkdir(parents=True, exist_ok=True)
                context.storage_state(path=str(output_state))
                _write(result_path, status="ok", detail=detail)
                browser_launch.close_context(browser, context)
                return 0

            _write(result_path, status=status, detail=detail)
            browser_launch.close_context(browser, context)
            return 2
    except Exception as exc:  # noqa: BLE001 - parent needs a result, not a traceback-only death
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

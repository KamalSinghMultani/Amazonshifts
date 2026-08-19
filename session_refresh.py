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

            # A cheap-ish check first. It is not perfect, so scheduled proactive
            # refreshes can force a login even when the shell still loads.
            check = doctor.check_portal_login(page, cfg["site"]["base_url"], settle_ms=1500)
            healthy = check.state == doctor.OK

            if healthy and not force_login:
                context.storage_state(path=str(output_state))
                _write(result_path, status="healthy", detail=check.detail)
                browser_launch.close_context(browser, context)
                return 0

            status, detail = login_flow.attempt(page, cfg["site"]["base_url"])
            if status == login_flow.OK:
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

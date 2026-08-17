"""One-time manual login. Run this first.

Opens a real browser window. YOU type the email, password, and OTP — this
script never sees, reads, or stores any of them. When you are logged in, come
back to the terminal and press Enter; the resulting cookies are saved to
auth_state.json (gitignored) and every other script reuses them.

    python save_session.py

Re-run it whenever the session expires and the watcher starts reporting that
it is logged out.
"""

from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

import browser_launch
from config import load_config


def main() -> int:
    cfg = load_config()
    site = cfg["site"]
    browser_cfg = cfg["browser"]
    out_path = Path(browser_cfg["storage_state"])

    print("Opening a browser window.")
    print("Log in to hiring.amazon.ca by hand — password and OTP included.")
    print("This script does not read or store your credentials.\n")
    print(f"Browser: {browser_launch.describe(browser_cfg)}")
    if browser_cfg.get("channel"):
        print(
            "If that browser is not installed, set browser.channel to null in "
            "config.yaml to fall back to Playwright's bundled Chromium.\n"
        )

    with sync_playwright() as playwright:
        try:
            # Always headed: the whole point is that a human logs in.
            browser, context = browser_launch.launch_context(
                playwright, browser_cfg, headless=False
            )
        except PlaywrightError as exc:
            print(f"\nCould not start the browser: {exc}\n")
            if browser_cfg.get("channel"):
                print(
                    f"browser.channel is {browser_cfg['channel']!r}. If that browser "
                    "is not installed, set it to null in config.yaml, or run:\n"
                    "    python -m playwright install chromium"
                )
            return 1

        page = context.pages[0] if context.pages else context.new_page()
        page.goto(site["job_search_url"], timeout=browser_cfg["nav_timeout_ms"])

        try:
            input("\nPress Enter here once you are fully logged in… ")
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled — nothing was saved.")
            browser_launch.close_context(browser, context)
            return 1

        context.storage_state(path=str(out_path))
        browser_launch.close_context(browser, context)

    try:
        out_path.chmod(0o600)
    except OSError:
        pass  # best effort; Windows will not honour this

    print(f"\nSession saved to {out_path} (permissions set to owner-only).")
    print("This file is your login. It is gitignored — keep it that way.")
    if browser_cfg.get("user_data_dir"):
        print(
            f"The browser profile in {browser_cfg['user_data_dir']}/ also holds your "
            "session and is reused on every run — equally sensitive, also gitignored."
        )
    print("\nNext: python watcher.py --check-selectors")
    return 0


if __name__ == "__main__":
    sys.exit(main())

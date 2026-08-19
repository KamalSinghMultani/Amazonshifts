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

import argparse
import sys
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

import browser_launch
from config import load_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="One-time manual login")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument(
        "--url",
        default=None,
        help="log in somewhere other than site.job_search_url — e.g. the US "
             "site, which shares the same auth service",
    )
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    site = cfg["site"]
    browser_cfg = cfg["browser"]
    out_path = Path(browser_cfg["storage_state"])
    target = args.url or site["job_search_url"]

    print("Opening a browser window.")
    print(f"Log in at {target} by hand — password and OTP included.")
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
        page.goto(target, timeout=browser_cfg["nav_timeout_ms"])

        # Browsing and applying are separate sessions on this site: job search
        # is public, so a signed-out browser looks perfectly healthy right up
        # until a hold fails. Checking the apply flow here is the only way to
        # know the login that matters actually took.
        print(
            "\nBEFORE pressing Enter: open any job, click 'Select schedule',\n"
            "then 'Apply' on a schedule. If you see the application rather than\n"
            "a login page, the hiring-portal session is real — that is the part\n"
            "that decides whether a slot can be held."
        )
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

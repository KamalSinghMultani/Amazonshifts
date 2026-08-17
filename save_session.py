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

from playwright.sync_api import sync_playwright

from config import load_config


def main() -> int:
    cfg = load_config()
    site = cfg["site"]
    browser_cfg = cfg["browser"]
    out_path = Path(browser_cfg["storage_state"])

    print("Opening a browser window.")
    print("Log in to hiring.amazon.ca by hand — password and OTP included.")
    print("This script does not read or store your credentials.\n")

    with sync_playwright() as playwright:
        # Always headed: the whole point is that a human logs in.
        browser = playwright.chromium.launch(
            headless=False,
            executable_path=browser_cfg.get("executable_path") or None,
        )
        context = browser.new_context(
            user_agent=browser_cfg.get("user_agent") or None,
            locale=browser_cfg.get("locale") or None,
            timezone_id=browser_cfg.get("timezone") or None,
        )
        page = context.new_page()
        page.goto(site["job_search_url"], timeout=browser_cfg["nav_timeout_ms"])

        try:
            input("\nPress Enter here once you are fully logged in… ")
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled — nothing was saved.")
            browser.close()
            return 1

        context.storage_state(path=str(out_path))
        browser.close()

    try:
        out_path.chmod(0o600)
    except OSError:
        pass  # best effort; Windows will not honour this

    print(f"\nSession saved to {out_path} (permissions set to owner-only).")
    print("This file is your login. It is gitignored — keep it that way.")
    print("\nNext: python watcher.py --check-selectors")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Browser launching, shared by save_session.py, api_sniffer.py and watcher.py.

Why this module exists: Amazon's login flow detects automated browsers. The
usual symptom is that everything works until the OTP step, and then the
"send verification code" step silently refuses — no error, it just never
completes. Playwright's bundled Chromium is trivially detectable:

  * it sets `navigator.webdriver = true`
  * it launches with `--enable-automation`, which also shows the
    "Chrome is being controlled by automated test software" banner
  * a fresh context has no history, no profile, and no device trust, so every
    login looks like a brand new device worth challenging

Three settings in config.yaml address that, in rough order of effectiveness:

  browser.channel        drive your REAL installed Chrome instead of the
                         bundled Chromium
  browser.user_data_dir  keep a persistent profile, so Amazon remembers the
                         device and stops re-challenging on every login
  browser.stealth        drop the automation flags and the webdriver property

None of this defeats a CAPTCHA or logs in for you — you still type your own
password and OTP by hand. It stops a legitimate manual login from being
misread as a bot.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

# Removing this arg also removes the "controlled by automated test software"
# infobar, which is itself a detection signal.
STEALTH_ARGS = ["--disable-blink-features=AutomationControlled"]
STEALTH_IGNORE_ARGS = ["--enable-automation"]

# navigator.webdriver is the single most-checked automation tell.
STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
"""


def launch_context(
    playwright,
    browser_cfg: dict,
    *,
    headless: bool | None = None,
    storage_state: str | None = None,
):
    """Launch a browser and return (browser, context).

    `browser` is None when a persistent profile is used — in that case the
    context owns the browser process. Always close with `close_context()`
    rather than assuming one or the other.
    """
    if headless is None:
        headless = bool(browser_cfg.get("headless", True))

    stealth = bool(browser_cfg.get("stealth", True))
    channel = browser_cfg.get("channel") or None
    executable_path = browser_cfg.get("executable_path") or None
    user_data_dir = browser_cfg.get("user_data_dir") or None

    launch_kwargs: dict = {"headless": headless}
    if channel:
        launch_kwargs["channel"] = channel
    if executable_path:
        launch_kwargs["executable_path"] = executable_path
    if stealth:
        launch_kwargs["args"] = list(STEALTH_ARGS)
        launch_kwargs["ignore_default_args"] = list(STEALTH_IGNORE_ARGS)

    context_kwargs = {
        "user_agent": browser_cfg.get("user_agent") or None,
        "locale": browser_cfg.get("locale") or None,
        "timezone_id": browser_cfg.get("timezone") or None,
    }

    if user_data_dir:
        # A persistent profile keeps cookies, localStorage and Amazon's device
        # trust between runs. storage_state is meaningless here — the profile
        # on disk IS the state.
        profile = Path(user_data_dir)
        profile.mkdir(parents=True, exist_ok=True)
        log.info("using persistent browser profile at %s", profile)
        context = playwright.chromium.launch_persistent_context(
            str(profile), **launch_kwargs, **context_kwargs
        )
        browser = None
    else:
        browser = playwright.chromium.launch(**launch_kwargs)
        context = browser.new_context(storage_state=storage_state, **context_kwargs)

    if stealth:
        context.add_init_script(STEALTH_SCRIPT)

    return browser, context


def close_context(browser, context) -> None:
    """Close whichever of the two actually owns the browser process."""
    try:
        if browser is not None:
            browser.close()
        else:
            context.close()
    except Exception as exc:  # noqa: BLE001 - shutdown must not raise
        log.debug("error while closing browser: %s", exc)


def describe(browser_cfg: dict) -> str:
    """One-line summary of how the browser will be launched, for logging."""
    channel = browser_cfg.get("channel") or "bundled chromium"
    profile = browser_cfg.get("user_data_dir") or "fresh context"
    stealth = "stealth on" if browser_cfg.get("stealth", True) else "stealth off"
    return f"{channel}, {profile}, {stealth}"

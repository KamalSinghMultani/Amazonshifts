"""
One-off diagnostic: open a direct US application URL, click Create Application,
log every POST request, and measure hold time.

If the US profile is not logged in, this script will open the login page
and wait for you to sign in manually before continuing.
"""

import json
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

APPLICATION_URL = (
    "https://hiring.amazon.com/application/us/?"
    "country=us&intcmpid=searchalljobscenter&jobId=JOB-US-0000019413"
    "&locale=en-US&scheduleId=SCH-US-0000735045#/consent"
)

PROFILE_DIR = "browser_profile_us" if Path("browser_profile_us").exists() else "browser_profile"

def main():
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR,
            headless=False,
            channel="chrome",
        )

        page = context.pages[0] if context.pages else context.new_page()

        def on_request(request):
            if request.method == "POST":
                print("\n=== POST REQUEST ===")
                print("URL:", request.url)
                print("Headers:", json.dumps(dict(request.headers), indent=2))
                try:
                    print("Body:", request.post_data)
                except Exception as exc:
                    print("Could not read body:", exc)

        page.on("request", on_request)

        print(f"Opening {APPLICATION_URL}")
        page.goto(APPLICATION_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)

        # If we landed on the login page, wait for the user to sign in
        login_wait_attempts = 0
        while "auth.hiring.amazon" in page.url and login_wait_attempts < 120:
            print("\n*** Login required. Please sign in manually in the Chrome window. ***")
            print("Waiting for you to finish logging in...")
            page.wait_for_timeout(2000)
            login_wait_attempts += 1
            # After login, Amazon should redirect back to the application URL
            if "application" in page.url:
                break

        if "auth.hiring.amazon" in page.url:
            print("Still on login page after waiting. Exiting.")
            context.close()
            return

        print("\nLogged in. Looking for application actions...")

        # Dismiss cookie/consent modal if present
        try:
            consent = page.locator("[data-test-id='consentBtn']")
            if consent.count() and consent.first.is_visible():
                consent.click(timeout=3000)
                page.wait_for_timeout(500)
        except Exception:
            pass

        # If there's a pre-consent "Next", click it
        try:
            next_btn = page.locator("[data-test-id='layout'] button:has-text('Next')")
            if next_btn.count() and next_btn.first.is_visible():
                next_btn.click(timeout=5000)
                page.wait_for_timeout(2000)
        except Exception:
            pass

        # Now wait for Create Application and click it, timing the hold
        start = time.perf_counter()
        try:
            create_btn = page.locator(
                "[data-test-id='layout'] button:has-text('Create Application')"
            )
            create_btn.wait_for(state="visible", timeout=20000)
            print("\nCreate Application visible. Clicking...")
            create_btn.click(timeout=5000)
            page.wait_for_timeout(5000)
        except Exception as exc:
            print(f"Could not click Create Application: {exc}")

        elapsed = time.perf_counter() - start
        print(f"\nHold attempt completed in {elapsed:.2f}s")

        try:
            text = page.inner_text("body")
            if "holding a spot" in text.lower():
                print("HOLD CONFIRMED")
            else:
                print("No confirmation banner. Page text after click:")
                print(text[:1000])
        except Exception:
            pass

        context.close()

if __name__ == "__main__":
    main()
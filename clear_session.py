from playwright.sync_api import sync_playwright

PROFILE_DIR = "browser_profile"

with sync_playwright() as p:
    context = p.chromium.launch_persistent_context(
        user_data_dir=PROFILE_DIR,
        headless=False,
        channel="chrome",
    )

    # Clear cookies
    context.clear_cookies()

    # Clear localStorage/sessionStorage on key domains
    for url in [
        "https://hiring.amazon.ca",
        "https://auth.hiring.amazon.com",
        "https://hiring.amazon.com",
    ]:
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded")
        page.evaluate("localStorage.clear(); sessionStorage.clear();")
        page.close()

    print("Cleared cookies and storage. You should now be fully logged out.")
    context.close()
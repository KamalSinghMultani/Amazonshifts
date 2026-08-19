from playwright.sync_api import sync_playwright

PROFILE_DIR = "browser_profile"

with sync_playwright() as p:
    context = p.chromium.launch_persistent_context(
        user_data_dir=PROFILE_DIR,
        headless=False,
        channel="chrome",
    )
    page = context.pages[0] if context.pages else context.new_page()
    page.goto("https://hiring.amazon.ca/app#/jobSearch")
    print("\nBrowser opened with your watcher profile.")
    print("Please log out manually from Amazon (use the account menu / sign out).")
    print("After you have logged out, close the browser window.\n")
    input("Press Enter here after you've closed the browser...")
    context.close()
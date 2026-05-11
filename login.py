from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    context = browser.new_context()
    page = context.new_page()
    
    page.goto("https://studio.learn.authoring.goacademy.wgu.edu/")
    
    print("Log in manually in the browser window.")
    print("Once you're fully logged in and can see the course, come back here and press Enter.")
    input()
    
    # Save the session
    context.storage_state(path="auth.json")
    print("Session saved to auth.json")
    
    browser.close()

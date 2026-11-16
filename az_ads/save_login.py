"""
One-time (and whenever Amazon logs the robot out):  python save_login.py

Opens a real browser window. YOU sign in to Amazon Ads (password + OTP) and open
the Products page; this script only saves the login cookies afterwards.

Creates (both are in .gitignore - never commit them):
  amazon_state.json      used when you run  python run.py  on this PC
  amazon_state.b64.txt   paste its content into the GitHub secret AMAZON_ADS_STORAGE_STATE
"""
import base64
import json
from pathlib import Path

from playwright.sync_api import sync_playwright

START = "https://advertising.amazon.in/campaign-manager/all-campaigns"
HERE = Path(__file__).resolve().parent
PROFILE = HERE / ".browser-profile"

with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(
        str(PROFILE), headless=False, viewport=None,
        args=["--start-maximized", "--disable-blink-features=AutomationControlled"])
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto(START)
    print("\n1. Sign in to Amazon Ads in the window that opened (tick 'Keep me signed in').")
    print("2. Click the arrow at the top-left of the page, then click 'Products'.")
    input("3. When the Products table is on screen, come back here and press ENTER ... ")

    page = ctx.pages[-1]
    if "advertising.amazon." not in page.url or "/ap/" in page.url:
        raise SystemExit(f"Not on the Amazon Ads console (current page: {page.url.split('?')[0]}). Nothing saved.")
    cookies = [c for c in ctx.cookies() if "amazon" in c["domain"]]
    state = {"cookies": cookies, "origins": [], "products_url": page.url}
    ctx.close()

text = json.dumps(state)
(HERE / "amazon_state.json").write_text(text, encoding="utf-8")
(HERE / "amazon_state.b64.txt").write_text(base64.b64encode(text.encode()).decode(), encoding="utf-8")
print(f"\nSaved {len(cookies)} cookies.")
print("Local runs   : amazon_state.json")
print("GitHub secret: copy the whole content of amazon_state.b64.txt into AMAZON_ADS_STORAGE_STATE")

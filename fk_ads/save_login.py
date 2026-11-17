"""
One-time (and whenever Flipkart logs the robot out):  python save_login.py

Opens a real browser window. YOU sign in to Flipkart Ads; this script only saves the
login session afterwards.

Creates (both are in .gitignore - never commit them):
  flipkart_state.json      used when you run  python run.py  on this PC
  flipkart_state.b64.txt   paste its content into the GitHub secret FLIPKART_ADS_STORAGE_STATE
"""
import base64
import json
from pathlib import Path

from playwright.sync_api import sync_playwright

START = "https://advertising.flipkart.com/"
HERE = Path(__file__).resolve().parent
PROFILE = HERE / ".browser-profile"

with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(
        str(PROFILE), headless=False, viewport=None,
        args=["--start-maximized", "--disable-blink-features=AutomationControlled"])
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto(START)
    print("\n1. Sign in to Flipkart Ads in the window that opened.")
    input("2. When the dashboard (Total Spend / Wallet) is on screen, come back here and press ENTER ... ")

    page = ctx.pages[-1]
    if "advertising.flipkart.com" not in page.url or "/login" in page.url:
        raise SystemExit(f"Not signed in to Flipkart Ads (current page: {page.url.split('?')[0]}). Nothing saved.")
    state = ctx.storage_state()
    state["cookies"] = [c for c in state["cookies"] if "flipkart" in c["domain"]]
    state["origins"] = [o for o in state.get("origins", []) if "flipkart" in o["origin"]]
    ctx.close()

text = json.dumps(state)
(HERE / "flipkart_state.json").write_text(text, encoding="utf-8")
(HERE / "flipkart_state.b64.txt").write_text(base64.b64encode(text.encode()).decode(), encoding="utf-8")
print(f"\nSaved {len(state['cookies'])} cookies.")
print("Local runs   : flipkart_state.json")
print("GitHub secret: copy the whole content of flipkart_state.b64.txt into FLIPKART_ADS_STORAGE_STATE")

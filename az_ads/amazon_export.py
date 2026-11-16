"""
Downloads the Amazon Ads  Campaign manager > Products  export for one day.

Login is NOT done here. It re-uses the cookies saved by save_login.py:
  * locally : amazon_state.json
  * GitHub  : secret AMAZON_ADS_STORAGE_STATE (base64 of that file)
If Amazon no longer accepts the cookies the run stops with LoginExpired -
run save_login.py again and update the secret.
"""
import base64
import json
import os
import re
from datetime import date
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / "amazon_state.json"
OUT = HERE / "out"
PRODUCTS = "https://advertising.amazon.in/cm/products"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


class LoginExpired(RuntimeError):
    pass


def load_state():
    raw = os.environ.get("AMAZON_ADS_STORAGE_STATE", "").strip()
    if raw:
        try:
            if not raw.startswith("{"):
                raw = base64.b64decode("".join(raw.split())).decode("utf-8")
            state = json.loads(raw)
            assert state.get("cookies")
            return state
        except Exception:
            raise LoginExpired("The AMAZON_ADS_STORAGE_STATE secret is not valid - paste the WHOLE "
                               "content of amazon_state.b64.txt into it, nothing else.")
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    if os.environ.get("GITHUB_ACTIONS"):
        raise LoginExpired("The AMAZON_ADS_STORAGE_STATE secret is missing or empty in this repository "
                           "(Settings > Secrets and variables > Actions > Repository secrets).")
    raise LoginExpired("No saved Amazon login. Run:  python save_login.py")


def _check_signed_in(page):
    if "/ap/signin" in page.url or "/ap/mfa" in page.url or "/ap/cvf" in page.url:
        raise LoginExpired("Amazon asked for sign-in / OTP - saved cookies are no longer accepted. "
                           "Run save_login.py again and update the AMAZON_ADS_STORAGE_STATE secret.")
    if "validateCaptcha" in page.url or page.locator("form[action*='validateCaptcha']").count():
        raise LoginExpired("Amazon showed a CAPTCHA to this machine - cannot continue automatically.")


def _open_products(page, products_url, log):
    # The side-menu "Products" entry is just this URL: /cm/products?entityId=<account>
    m = re.search(r"entityId=[^&#]+", products_url or "")
    url = PRODUCTS + ("?" + m.group(0) if m else "")
    page.goto(url, wait_until="domcontentloaded", timeout=90_000)
    date_btn = page.locator("[id='products_view:products_view:dateRangeFilter:openContainer']")
    export = page.locator("[id='products_view:products_view:export']")
    try:
        date_btn.wait_for(state="visible", timeout=90_000)
    except PWTimeout:
        _check_signed_in(page)
        raise RuntimeError("Products page did not load (date-range button not found). "
                           "See out/failure.png - Amazon may have changed the page.")
    _check_signed_in(page)
    export.wait_for(state="visible", timeout=60_000)
    return date_btn, export


def _set_date(page, date_btn, day: date, log):
    """Pick a single day in the date-range picker and confirm the button shows it."""
    want = f"{day:%b} {day.day}, {day.year}"                     # Nov 12, 2026
    shown = lambda: re.sub(r"\s+", " ", date_btn.inner_text())
    single = lambda: want in shown() and not re.search(r"\d{4}\s*[-–]", shown()) and shown().count(",") == 1
    if single():
        log(f"Date range already: {shown()}")
        return
    date_btn.click()
    # pick the day in the calendar
    label = f"{day:%A}, {day:%B} {day.day}, {day.year}"          # Saturday, November 14, 2026
    cell = page.locator(f"td[aria-label*='{label}']:visible").first
    for _ in range(12):                                          # older month -> page backwards
        page.wait_for_timeout(600)
        if cell.count():
            break
        page.locator("[aria-label*='previous month']:visible").first.click(timeout=10_000)
    if "Not available" in (cell.get_attribute("aria-label") or ""):
        raise RuntimeError(f"Amazon's calendar does not allow {want}")
    cell.click(timeout=15_000)          # one click = start and end on the same day
    page.wait_for_timeout(700)
    page.locator("button", has_text=re.compile(r"^\s*Apply\s*$")).first.click(timeout=15_000)
    for _ in range(20):                        # the table reloads after Apply
        page.wait_for_timeout(500)
        if single():
            break
    if not single():
        raise RuntimeError(f"Date range shows '{shown()}' but '{want}' was expected - "
                           "not exporting the wrong day")
    log(f"Date range set: {shown()}")


def download_products(day: date, headless=True, log=print) -> Path:
    OUT.mkdir(exist_ok=True)
    state = load_state()
    products_url = state.pop("products_url", "") or os.environ.get("AMAZON_ADS_PRODUCTS_URL", "")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless,
                                    args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(storage_state=state, accept_downloads=True, user_agent=UA,
                                  locale="en-IN", timezone_id="Asia/Kolkata",
                                  viewport={"width": 1680, "height": 950})
        page = ctx.new_page()
        try:
            date_btn, export = _open_products(page, products_url, log)
            _set_date(page, date_btn, day, log)
            page.wait_for_timeout(5000)                    # let the table finish reloading
            with page.expect_download(timeout=120_000) as dl:
                export.click()
            suffix = Path(dl.value.suggested_filename).suffix or ".csv"
            dest = OUT / f"products_{day.isoformat()}{suffix}"
            dl.value.save_as(dest)
            log(f"Export saved: {dest.name}")
            return dest
        except Exception:
            try:                                   # evidence for the failed run (kept as artifact)
                page.screenshot(path=str(OUT / "failure.png"), full_page=False)
                (OUT / "failure_url.txt").write_text(page.url.split("?")[0], encoding="utf-8")
            except Exception:
                pass
            raise
        finally:
            ctx.close()
            browser.close()

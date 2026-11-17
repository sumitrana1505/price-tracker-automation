"""
Downloads the Flipkart Ads  Reports > Other Reports > Consolidated FSN Report  for one day,
once per ad account (Philips, Coocaa).

Login is NOT typed here by default. It re-uses the session saved by save_login.py:
  * locally : flipkart_state.json
  * GitHub  : secret FLIPKART_ADS_STORAGE_STATE (base64 of that file)
If Flipkart no longer accepts the session the run stops with LoginExpired - unless the optional
FLIPKART_ADS_EMAIL / FLIPKART_ADS_PASSWORD secrets are set, then it signs in with those.
"""
import base64
import json
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / "flipkart_state.json"
OUT = HERE / "out"
HOME = "https://advertising.flipkart.com/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


class LoginExpired(RuntimeError):
    pass


def load_state():
    raw = os.environ.get("FLIPKART_ADS_STORAGE_STATE", "").strip()
    if raw:
        try:
            if not raw.startswith("{"):
                raw = base64.b64decode("".join(raw.split())).decode("utf-8")
            state = json.loads(raw)
            assert state.get("cookies")
            return state
        except Exception:
            raise LoginExpired("The FLIPKART_ADS_STORAGE_STATE secret is not valid - paste the WHOLE "
                               "content of flipkart_state.b64.txt into it, nothing else.")
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    if os.environ.get("FLIPKART_ADS_EMAIL") and os.environ.get("FLIPKART_ADS_PASSWORD"):
        return None                                   # no saved session, sign in with the secrets
    if os.environ.get("GITHUB_ACTIONS"):
        raise LoginExpired("The FLIPKART_ADS_STORAGE_STATE secret is missing or empty in this repository "
                           "(Settings > Secrets and variables > Actions > Repository secrets).")
    raise LoginExpired("No saved Flipkart login. Run:  python save_login.py")


def _on_login_page(page):
    return "/login" in page.url or page.locator("input[type='password']:visible").count() > 0


def _ensure_signed_in(page, log):
    page.goto(HOME, wait_until="domcontentloaded", timeout=90_000)
    try:
        page.wait_for_load_state("networkidle", timeout=30_000)
    except PWTimeout:
        pass
    if not _on_login_page(page):
        return
    email, pwd = os.environ.get("FLIPKART_ADS_EMAIL", ""), os.environ.get("FLIPKART_ADS_PASSWORD", "")
    if not (email and pwd):
        raise LoginExpired("Flipkart Ads asked for sign-in - the saved session is no longer accepted. "
                           "Run save_login.py again and update the FLIPKART_ADS_STORAGE_STATE secret.")
    log("Saved session not accepted - signing in with FLIPKART_ADS_EMAIL / FLIPKART_ADS_PASSWORD")
    page.get_by_placeholder("Enter email").fill(email)
    page.get_by_placeholder("Enter password").fill(pwd)
    page.locator("button[type='submit']").first.click()
    try:
        page.wait_for_url(lambda u: "/login" not in u, timeout=60_000)
    except PWTimeout:
        raise LoginExpired("Sign-in with FLIPKART_ADS_EMAIL / FLIPKART_ADS_PASSWORD did not go through "
                           "(wrong password, OTP or CAPTCHA) - run save_login.py instead.")


def _visible(page, text):
    """Last visible element whose whole text is `text` (None if there is none)."""
    loc = page.get_by_text(text, exact=True)
    for i in range(loc.count() - 1, -1, -1):
        if loc.nth(i).is_visible():
            return loc.nth(i)
    return None


def _switch_account(page, name, all_names, log):
    """Top-right account button -> 'Switch to account' -> click the account name."""
    page.wait_for_function(
        "names => names.some(n => document.body.innerText.includes(n))", arg=all_names, timeout=90_000)
    if _visible(page, name):
        log(f"Account: {name}")
        return
    current = next((el for n in all_names if (el := _visible(page, n))), None)
    if current is None:
        raise RuntimeError(f"None of the accounts {all_names} is shown at the top right - "
                           "check 'accounts' in config.json")
    current.click()
    page.wait_for_timeout(1000)
    target = _visible(page, name)
    if target is None:
        raise RuntimeError(f"Account '{name}' is not in the 'Switch to account' list")
    target.click()
    page.wait_for_timeout(3000)
    try:
        page.wait_for_load_state("networkidle", timeout=30_000)
    except PWTimeout:
        pass
    if not _visible(page, name):
        raise RuntimeError(f"Switching to account '{name}' did not work")
    log(f"Account: {name}")


def _open_other_reports(page):
    link = page.locator("a[href*='report' i]:visible").first      # left panel: Reports icon
    if link.count():
        link.click()
    else:
        page.goto(HOME + "ad-account/reports" + (("?" + page.url.split("?", 1)[1]) if "?" in page.url else ""),
                  wait_until="domcontentloaded", timeout=90_000)
    tab = page.get_by_text("Other Reports", exact=True).first
    tab.wait_for(state="visible", timeout=60_000)
    tab.click()
    page.get_by_text("Report Type", exact=True).first.wait_for(state="visible", timeout=60_000)


def _field(page, label):
    """The box (label + control) of one form field of the Other Reports form."""
    return page.get_by_text(label, exact=True).first.locator("xpath=..")


def _choose(page, label, option, log):
    box = _field(page, label)
    if box.locator("select").count():
        box.locator("select").first.select_option(label=option)
        return
    shown = re.sub(r"\s+", " ", box.inner_text()).replace(label, "", 1).strip()
    if shown == option:
        return
    box.locator("xpath=./*[last()]").click()                      # the dropdown under the label
    page.wait_for_timeout(700)
    opt = _visible(page, option)
    if opt is None:
        raise RuntimeError(f"'{label}' has no option '{option}'")
    opt.click()
    page.wait_for_timeout(700)
    log(f"{label}: {option}")


def _set_date(page, day: date, today: date):
    preset = {today - timedelta(days=1): "Yesterday", today: "Today"}.get(day)
    if preset is None:
        raise RuntimeError(f"Only yesterday can be downloaded automatically for now. For {day}: download "
                           "the two reports by hand and run  python run.py --date ... --file A --file B")
    _field(page, "Date").locator("xpath=./*[last()]").click()
    page.wait_for_timeout(700)
    item = _visible(page, preset)
    if item is None:
        raise RuntimeError(f"Date picker has no '{preset}' entry")
    item.click()
    page.wait_for_timeout(500)
    done = _visible(page, "Done")
    if done:
        done.click()
    page.wait_for_timeout(700)


def download_reports(day: date, cfg, headless=True, log=print):
    """-> [Path, ...] one downloaded report per account in cfg['accounts']."""
    OUT.mkdir(exist_ok=True)
    state = load_state()
    today = datetime.now(ZoneInfo(cfg.get("timezone", "Asia/Kolkata"))).date()
    files = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless,
                                    args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(storage_state=state, accept_downloads=True, user_agent=UA,
                                  locale="en-IN", timezone_id="Asia/Kolkata",
                                  viewport={"width": 1680, "height": 950})
        page = ctx.new_page()
        try:
            _ensure_signed_in(page, log)
            for n, account in enumerate(cfg["accounts"], start=1):
                _switch_account(page, account, cfg["accounts"], log)
                _open_other_reports(page)
                for label, option in cfg["report"].items():
                    _choose(page, label, option, log)
                _set_date(page, day, today)
                with page.expect_download(timeout=180_000) as dl:
                    page.get_by_role("button", name=re.compile(r"Download")).first.click()
                suffix = Path(dl.value.suggested_filename).suffix or ".csv"
                dest = OUT / f"fsn_{day.isoformat()}_{n}{suffix}"
                dl.value.save_as(dest)
                log(f"Report saved: {dest.name}")
                files.append(dest)
            return files
        except Exception:
            try:                                   # evidence for the failed run (never uploaded)
                page.screenshot(path=str(OUT / "failure.png"), full_page=False)
                if not os.environ.get("GITHUB_ACTIONS"):
                    (OUT / "failure.html").write_text(page.content(), encoding="utf-8")
            except Exception:
                pass
            raise
        finally:
            ctx.close()
            browser.close()

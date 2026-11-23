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
            if os.environ.get("FLIPKART_ADS_EMAIL") and os.environ.get("FLIPKART_ADS_PASSWORD"):
                return None                           # broken session secret: sign in with the password
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


def page_state(page):
    """Short description of the page for the public log: no digits, so no ads numbers."""
    try:
        text = re.sub(r"\s+", " ", page.locator("body").inner_text(timeout=5_000)).strip()
        text = re.sub(r"\d", "#", text)[:160]
        login = page.locator("input[type='password']").count() > 0
        return (f"page '{page.url.split('?')[0].replace(HOME, '/')}' title '{page.title()[:40]}' "
                f"login-form={login} text: '{text}'")
    except Exception as e:
        return f"page state unavailable ({type(e).__name__})"


def _wait_dashboard_or_login(page, accounts, seconds=90):
    """'dashboard' once an account name is shown, 'login' once the login form is shown."""
    js = """names => {
        if (document.querySelector("input[type='password']")) return 'login';
        const t = document.body ? document.body.innerText : '';
        return names.some(n => t.includes(n)) ? 'dashboard' : '';
    }"""
    try:
        return page.wait_for_function(js, arg=accounts, timeout=seconds * 1000, polling=1000).json_value()
    except PWTimeout:
        return ""


def _ensure_signed_in(page, accounts, log):
    page.goto(HOME, wait_until="domcontentloaded", timeout=90_000)
    seen = _wait_dashboard_or_login(page, accounts)
    if seen == "dashboard":
        return
    if not seen:
        page.reload(wait_until="domcontentloaded", timeout=90_000)      # one retry of a slow/blank load
        seen = _wait_dashboard_or_login(page, accounts)
        if seen == "dashboard":
            return
        if not seen:
            raise RuntimeError("Flipkart Ads showed neither the dashboard nor the login page within "
                               "3 minutes: " + page_state(page))
    if not _on_login_page(page):
        return
    email, pwd = os.environ.get("FLIPKART_ADS_EMAIL", ""), os.environ.get("FLIPKART_ADS_PASSWORD", "")
    if not (email and pwd):
        raise LoginExpired("Flipkart Ads asked for sign-in - the saved session is no longer accepted. "
                           "Add the FLIPKART_ADS_EMAIL and FLIPKART_ADS_PASSWORD secrets (automatic "
                           "sign-in), or run save_login.py again and update FLIPKART_ADS_STORAGE_STATE.")
    log("Saved session not accepted - signing in with FLIPKART_ADS_EMAIL / FLIPKART_ADS_PASSWORD")
    page.get_by_placeholder("Enter email").first.fill(email)
    page.get_by_placeholder("Enter password").first.fill(pwd)
    page.locator("button[type='submit']").first.click()
    # signed in only once the dashboard shows an account name; an OTP / verification page,
    # a CAPTCHA or "wrong password" leaves us elsewhere - say what Flipkart showed (digits masked)
    js = "names => names.some(n => (document.body ? document.body.innerText : '').includes(n))"
    try:
        page.wait_for_function(js, arg=accounts, timeout=90_000, polling=1000)
    except PWTimeout:
        raise LoginExpired("Sign-in with FLIPKART_ADS_EMAIL / FLIPKART_ADS_PASSWORD did not reach the "
                           "dashboard (wrong password, OTP or CAPTCHA?): " + page_state(page))
    log("Signed in with email / password")


def _visible(page, text):
    """Last visible element whose whole text is `text` (None if there is none)."""
    loc = page.get_by_text(text, exact=True)
    for i in range(loc.count() - 1, -1, -1):
        if loc.nth(i).is_visible():
            return loc.nth(i)
    return None


TOUR_BUTTONS = ("Done!", "Got it", "Got it!", "Okay", "OK", "Skip", "Skip tour", "Close", "Next")


def _dismiss_tours(page, log=print):
    """Close Flipkart's 'what's new' tour bubbles. A fresh browser (every GitHub run) gets them,
    and their grey overlay swallows every click."""
    for _ in range(6):
        page.wait_for_timeout(400)
        overlay = page.locator("[class*='MouseEventsBlockingOverlay']")
        btn = next((b for t in TOUR_BUTTONS if (b := _visible(page, t))), None)
        if btn is None:
            if overlay.count() and overlay.first.is_visible():
                page.keyboard.press("Escape")
                continue
            return
        try:
            btn.click(timeout=5_000)
        except PWTimeout:
            btn.click(timeout=5_000, force=True)
        log("Closed a Flipkart tour pop-up")


def _click(page, loc, log=print, tries=5):
    """Click; when a tour bubble pops up in the way (it can come late on a slow machine),
    close it and click again."""
    for i in range(tries):
        try:
            loc.click(timeout=8_000)
            return
        except PWTimeout:
            if i == tries - 1:
                raise
            _dismiss_tours(page, log)


def _wait_visible(page, text, seconds=15):
    """Wait until an element with exactly this text is visible; return it (or None)."""
    for _ in range(int(seconds * 2)):
        if (el := _visible(page, text)) is not None:
            return el
        page.wait_for_timeout(500)
    return None


def _switch_account(page, name, all_names, log):
    """Top-right account button -> 'Switch to account' -> click the account name."""
    page.wait_for_function(
        "names => names.some(n => document.body.innerText.includes(n))", arg=all_names, timeout=90_000)
    if _visible(page, name):
        log(f"Account: {name}")
        return
    target = None
    for attempt in range(3):                 # GitHub's machines are slow: wait for the menu, retry
        _dismiss_tours(page, log)
        current = next((el for n in all_names if (el := _visible(page, n))), None)
        if current is None:
            raise RuntimeError(f"None of the accounts {all_names} is shown at the top right - "
                               "check 'accounts' in config.json")
        _click(page, current, log)
        if (target := _wait_visible(page, name, 15)) is not None:   # 'Switch to account' list
            break
        page.keyboard.press("Escape")
        page.wait_for_timeout(1000)
    if target is None:
        raise RuntimeError(f"Account '{name}' is not in the 'Switch to account' list "
                           "(opened the account menu 3 times)")
    _click(page, target, log)
    page.wait_for_timeout(3000)
    try:
        page.wait_for_load_state("networkidle", timeout=30_000)
    except PWTimeout:
        pass
    others = [n for n in all_names if n != name]
    for i in range(40):                      # up to 20 s: top right shows the new account only
        if _visible(page, name) and not any(_visible(page, n) for n in others):
            break
        if i % 10 == 9:
            page.keyboard.press("Escape")    # account menu left open
        page.wait_for_timeout(500)
    else:
        raise RuntimeError(f"Switching to account '{name}' did not work")
    log(f"Account: {name}")


def _open_other_reports(page, log=print):
    link = page.locator("a[href*='report' i]:visible").first      # left panel: Reports icon
    if link.count():
        _click(page, link, log)
    else:
        page.goto(HOME + "ad-account/reports" + (("?" + page.url.split("?", 1)[1]) if "?" in page.url else ""),
                  wait_until="domcontentloaded", timeout=90_000)
    tab = page.get_by_text("Other Reports", exact=True).first
    tab.wait_for(state="visible", timeout=60_000)
    _click(page, tab, log)
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
    _click(page, box.locator("xpath=./*[last()]"), log)           # the dropdown under the label
    opt = _wait_visible(page, option, 10)
    if opt is None:
        raise RuntimeError(f"'{label}' has no option '{option}'")
    _click(page, opt, log)
    page.wait_for_timeout(700)
    log(f"{label}: {option}")


def _set_date(page, day: date, today: date, log=print):
    preset = {today - timedelta(days=1): "Yesterday", today: "Today"}.get(day)
    if preset is None:
        raise RuntimeError(f"Only yesterday can be downloaded automatically for now. For {day}: download "
                           "the two reports by hand and run  python run.py --date ... --file A --file B")
    _click(page, _field(page, "Date").locator("xpath=./*[last()]"), log)
    item = _wait_visible(page, preset, 10)
    if item is None:
        raise RuntimeError(f"Date picker has no '{preset}' entry")
    _click(page, item, log)
    done = _wait_visible(page, "Done", 3)
    if done:
        _click(page, done, log)
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
        step = "sign in"
        try:
            _ensure_signed_in(page, cfg["accounts"], log)
            for n, account in enumerate(cfg["accounts"], start=1):
                step = f"switch to account {n}"
                _dismiss_tours(page, log)
                _switch_account(page, account, cfg["accounts"], log)
                step = f"open Other Reports (account {n})"
                _dismiss_tours(page, log)
                _open_other_reports(page, log)
                for label, option in cfg["report"].items():
                    step = f"set '{label}' (account {n})"
                    _dismiss_tours(page, log)
                    _choose(page, label, option, log)
                step = f"set Date (account {n})"
                _dismiss_tours(page, log)
                _set_date(page, day, today, log)
                step = f"Download (account {n})"
                _dismiss_tours(page, log)
                with page.expect_download(timeout=180_000) as dl:
                    _click(page, page.get_by_role("button", name=re.compile(r"Download")).first, log)
                suffix = Path(dl.value.suggested_filename).suffix or ".csv"
                dest = OUT / f"fsn_{day.isoformat()}_{n}{suffix}"
                dl.value.save_as(dest)
                log(f"Report saved: {dest.name}")
                files.append(dest)
            return files
        except LoginExpired:
            raise
        except Exception as e:
            try:                                   # evidence for the failed run (never uploaded)
                page.screenshot(path=str(OUT / "failure.png"), full_page=False)
                if not os.environ.get("GITHUB_ACTIONS"):
                    (OUT / "failure.html").write_text(page.content(), encoding="utf-8")
            except Exception:
                pass
            first = (str(e).splitlines() or [""])[0][:200]
            state = "" if "page '" in first else " | " + page_state(page)
            raise RuntimeError(f"Flipkart step '{step}' failed: {type(e).__name__}: {first}{state}") from e
        finally:
            ctx.close()
            browser.close()

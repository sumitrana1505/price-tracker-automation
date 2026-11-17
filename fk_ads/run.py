"""
Daily Flipkart Ads report:
  Consolidated FSN Report (yesterday) of the Philips + Coocaa accounts
  ->  Units Sold / Clicks per model  ->  Google Sheet tab "Ads_Data"

  python run.py                       yesterday (India time), full run
  python run.py --dry-run             show what would be written, change nothing in the sheet
  python run.py --skip-sheet          only download + print the numbers
  python run.py --show                watch the browser (local debugging)
  python run.py --date 2026-11-13 --file philips.csv --file coocaa.csv
                                      skip Flipkart, use reports you downloaded by hand
"""
import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.resolve().parent / "az_ads"))     # shared Google Sheet logic

import sheet_update  # noqa: E402
import fk_parse  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=os.environ.get("CUSTOM_DATE", "").strip() or None,
                    help="YYYY-MM-DD (default: yesterday)")
    ap.add_argument("--file", action="append", help="use this report instead of downloading (repeatable)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-sheet", action="store_true", help="only download + print the numbers")
    ap.add_argument("--show", action="store_true", help="visible browser window")
    a = ap.parse_args()

    cfg = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
    today = datetime.now(ZoneInfo(cfg.get("timezone", "Asia/Kolkata"))).date()
    day = date.fromisoformat(a.date) if a.date else today - timedelta(days=1)
    print(f"Report date: {sheet_update.date_label(day)}")

    if a.file:
        exports = [Path(f) for f in a.file]
    else:
        if day >= today:
            sys.exit("The day is not over yet - only yesterday or older can be reported.")
        import flipkart_export
        try:
            exports = flipkart_export.download_reports(day, cfg, headless=not a.show)
        except flipkart_export.LoginExpired as e:
            sys.exit(f"LOGIN NEEDED: {e}")

    # every file must say (Start Time / End Time) that it covers exactly this day
    data = fk_parse.merge(fk_parse.parse_report(f, expect_day=day) for f in exports)
    print(f"Reports: {len(exports)}, FSNs with ads: {len(data)}")

    if a.skip_sheet:
        for sec in cfg["sections"]:
            for model, fsn in sec["models"].items():
                d = sheet_update.lookup(data, fsn)
                print(f"  {model:14} units={d['orders']:<4} clicks={d['clicks']}")
        return
    ws = sheet_update.open_worksheet(cfg)
    sheet_update.update_sheet(ws, cfg, data, day, dry_run=a.dry_run)


if __name__ == "__main__":
    if os.environ.get("ADS_QUIET", "") != "1":
        main()
    else:                       # public CI log: short error instead of a full traceback
        try:
            main()
        except SystemExit as e:
            if e.code not in (0, None):
                print(f"::error title=FK_Ads_data::{e.code}")     # shown on the run's summary page
            raise
        except Exception as e:
            msg = str(e).splitlines()
            text = f"FAILED: {type(e).__name__}: {msg[0][:300] if msg else ''}"
            print(f"::error title=FK_Ads_data::{text}")
            sys.exit(text)

"""
Daily Amazon Ads report:  export Products (yesterday)  ->  Units/Clicks per model  ->  Google Sheet

  python run.py                       yesterday (India time), full run
  python run.py --date 2026-11-13     a specific day
  python run.py --file export.csv     skip Amazon, use an export you downloaded by hand
  python run.py --dry-run             show what would be written, change nothing in the sheet
  python run.py --show                watch the browser (local debugging)
"""
import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import sheet_update

HERE = Path(__file__).parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=os.environ.get("CUSTOM_DATE", "").strip() or None,
                    help="YYYY-MM-DD (default: yesterday)")
    ap.add_argument("--file", help="use this export file instead of downloading")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-sheet", action="store_true", help="only download + print the numbers")
    ap.add_argument("--show", action="store_true", help="visible browser window")
    a = ap.parse_args()

    cfg = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
    today = datetime.now(ZoneInfo(cfg.get("timezone", "Asia/Kolkata"))).date()
    day = date.fromisoformat(a.date) if a.date else today - timedelta(days=1)
    print(f"Report date: {sheet_update.date_label(day)}")

    if a.file:
        export = Path(a.file)
    else:
        if day >= today:
            sys.exit("The day is not over yet - only yesterday or older can be reported.")
        import amazon_export
        try:
            export = amazon_export.download_products(day, headless=not a.show)
        except amazon_export.LoginExpired as e:
            sys.exit(f"LOGIN NEEDED: {e}")

    data = sheet_update.parse_export(export)
    print(f"Export rows: {len(data)}")

    if a.skip_sheet:
        for sec in cfg["sections"]:
            for model, asin in sec["models"].items():
                d = sheet_update.lookup(data, asin)
                print(f"  {model:14} units={d['orders']:<4} clicks={d['clicks']}")
        return
    ws = sheet_update.open_worksheet(cfg)
    sheet_update.update_sheet(ws, cfg, data, day, dry_run=a.dry_run)


if __name__ == "__main__":
    if os.environ.get("ADS_QUIET", "") != "1":
        main()
    else:                       # public CI log: short, scrubbed error instead of a full traceback
        try:
            main()
        except Exception as e:
            msg = re.sub(r"entityId=[^\s&'\"]+", "entityId=***", str(e)).splitlines()
            sys.exit(f"FAILED: {type(e).__name__}: {msg[0][:300] if msg else ''}")

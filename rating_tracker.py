"""
Rating Tracker - daily ratings into the 'Ratings Tracker' Google Sheet tab
==========================================================================
Sheet layout (one row per product, one column per day):

    A Channel | B Brand | C Sales Model | D product link | E.. one date column per day

Every run:
  1. finds today's date column in row 1, or adds a new one right after the last date
     (the new column takes the formatting of the column before it),
  2. fetches the current star rating of every product link (Flipkart + Amazon),
  3. writes the ratings into today's column,
  4. highlights the cell Light Green 3 when the rating went up against the previous
     column and Light Red 3 when it dropped.

A product whose rating could not be fetched is left blank (an existing value is never erased).

Environment:
  GDRIVE_SERVICE_ACCOUNT_JSON   service account credentials (same as the price tracker)
  RATING_SPREADSHEET_ID         spreadsheet that holds the tab (falls back to GSHEET_SPREADSHEET_ID)
  RATING_SHEET                  tab name, default 'Ratings Tracker'
  CUSTOM_DATE                   optional date to fill instead of today, as YYYY-MM-DD
  TEST_LIMIT                    optional: only the first N products
"""

import argparse
import logging
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

if sys.platform == "win32":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    for stream in (sys.stdout, sys.stderr):
        if stream and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S", handlers=[logging.StreamHandler(sys.stdout)])
logger = logging.getLogger("RatingTracker")

DEFAULT_SHEET = "Ratings Tracker"
HEADER_ROW = 1
FIRST_DATA_ROW = 2
COL_LINK = 4            # D: product link
FIRST_DATE_COL = 5      # E: first date column
SHEETS_EPOCH = date(1899, 12, 30)     # Google Sheets day 0
DATE_FORMATS = ("%Y/%m/%d", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d-%b-%Y", "%d %b %Y", "%m/%d/%Y")


def col_to_letter(col_idx: int) -> str:
    result = ""
    while col_idx > 0:
        col_idx, rem = divmod(col_idx - 1, 26)
        result = chr(65 + rem) + result
    return result


def today_ist() -> date:
    return datetime.now(timezone(timedelta(hours=5, minutes=30))).date()


def header_to_date(val: Any) -> Optional[date]:
    """A row-1 cell (date serial number or text) -> date, or None if it is not a date."""
    if isinstance(val, bool) or val is None or val == "":
        return None
    if isinstance(val, (int, float)):
        return SHEETS_EPOCH + timedelta(days=int(val)) if 20000 < val < 80000 else None
    text = str(val).strip().split(" ")[0] if re.match(r"\d{4}[-/]", str(val).strip()) else str(val).strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def find_date_column(header: List[Any], target: date):
    """Returns (column, is_new). Existing column for `target`, else the one after the last used header."""
    last_used = FIRST_DATE_COL - 1
    for idx, val in enumerate(header, start=1):
        if idx < FIRST_DATE_COL or val is None or str(val).strip() == "":
            continue
        last_used = idx
        if header_to_date(val) == target:
            return idx, False
    return last_used + 1, True


def channel_of(link: str) -> str:
    low = link.lower()
    if "flipkart" in low:
        return "flipkart"
    if "amazon" in low or "amzn" in low:
        return "amazon"
    return ""


def clean_rating(val: Any) -> Optional[float]:
    try:
        r = float(val)
    except (TypeError, ValueError):
        return None
    return r if 1.0 <= r <= 5.0 else None


def fetch_ratings(products: List[Dict[str, Any]]) -> Dict[int, float]:
    """products: [{'row', 'link', 'model'}] -> {row: rating}"""
    from scrapers import FlipkartScraper, normalize_amazon_url
    import amazon_mobile

    ratings: Dict[int, float] = {}

    # ---- Amazon: all products concurrently (one mobile page each, no pincode needed for ratings)
    amazon = {}
    for p in products:
        if channel_of(p["link"]) == "amazon":
            m = re.search(r"/(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})", normalize_amazon_url(p["link"]) or "")
            if m:
                amazon[p["row"]] = m.group(1)
            else:
                logger.warning(f"  Row {p['row']} | {p['model']}: no ASIN in Amazon link")
    if amazon:
        logger.info(f"[Amazon] Fetching ratings for {len(set(amazon.values()))} products...")
        try:
            res = amazon_mobile.fetch_ratings(list(amazon.values()), log=logger.info)
        except Exception as e:
            logger.error(f"[Amazon] Rating fetch failed: {e}")
            res = {}
        for row, asin in amazon.items():
            r = clean_rating((res.get(asin) or {}).get("rating"))
            if r is not None:
                ratings[row] = r

    # ---- Flipkart: one by one (the scraper adds its own polite delay)
    flipkart = [p for p in products if channel_of(p["link"]) == "flipkart"]
    if flipkart:
        logger.info(f"[Flipkart] Fetching ratings for {len(flipkart)} products...")
        fk = FlipkartScraper()
        for i, p in enumerate(flipkart, start=1):
            r = None
            try:
                data = fk.scrape(p["link"], model=p["model"])
                r = clean_rating(data.rating)
                if r is None and data.error_msg:
                    logger.warning(f"  [Flipkart {i}/{len(flipkart)}] Row {p['row']} | {p['model']}: {data.error_msg}")
            except Exception as e:
                logger.warning(f"  [Flipkart {i}/{len(flipkart)}] Row {p['row']} | {p['model']}: {e}")
            if r is not None:
                ratings[p["row"]] = r
            logger.info(f"  [Flipkart {i}/{len(flipkart)}] Row {p['row']} | {p['model']} | rating={r}")
    return ratings


def column_backgrounds(sh, ws, col: int, first_row: int, last_row: int) -> Dict[int, Dict[str, float]]:
    """{row: background colour as displayed} for one column of the sheet."""
    letter = col_to_letter(col)
    title = ws.title.replace("'", "''")
    meta = sh.fetch_sheet_metadata(params={
        "ranges": [f"'{title}'!{letter}{first_row}:{letter}{last_row}"],
        "fields": "sheets(data(rowData(values(effectiveFormat(backgroundColor)))))",
    })
    row_data = meta["sheets"][0]["data"][0].get("rowData", [])
    out = {}
    for r in range(first_row, last_row + 1):
        i = r - first_row
        values = row_data[i].get("values", []) if i < len(row_data) else []
        bg = (values[0].get("effectiveFormat", {}).get("backgroundColor") if values else None) or {}
        # the API leaves out channels that are 0; no colour at all means plain white
        out[r] = ({"red": bg.get("red", 0.0), "green": bg.get("green", 0.0), "blue": bg.get("blue", 0.0)}
                  if bg else {"red": 1.0, "green": 1.0, "blue": 1.0})
    return out


def highlight_rating_changes(sh, ws, col: int, ratings: Dict[int, float], last_row: int):
    """
    Compares today's rating with the previous column (the previous day):
      rating increased -> Light Green 3 fill, rating dropped -> Light Red 3 fill.
    Every other cell of today's column gets the sheet's normal background for its row
    (taken from the first date column), so yesterday's highlights are not carried over
    and the sheet's own formatting stays as it is.
    """
    from gsheet_engine import LIGHT_RED_3, LIGHT_GREEN_3

    prev_col = col - 1
    if prev_col < FIRST_DATE_COL:
        logger.info("No previous date column - skipping rating-change highlighting.")
        return
    prev_letter = col_to_letter(prev_col)
    prev_rows = ws.get(f"{prev_letter}{FIRST_DATA_ROW}:{prev_letter}{last_row}",
                       value_render_option="UNFORMATTED_VALUE")

    colours: Dict[int, Dict[str, float]] = {}
    up = down = 0
    for r, now in ratings.items():
        i = r - FIRST_DATA_ROW
        prev = clean_rating(prev_rows[i][0]) if i < len(prev_rows) and prev_rows[i] else None
        if prev is None:
            continue
        if now > prev:
            colours[r] = LIGHT_GREEN_3
            up += 1
        elif now < prev:
            colours[r] = LIGHT_RED_3
            down += 1

    try:
        base = column_backgrounds(sh, ws, FIRST_DATE_COL, FIRST_DATA_ROW, last_row)
        for r, colour in base.items():
            colours.setdefault(r, colour)
    except Exception as e:
        logger.warning(f"Note on reading the sheet's row colours (unchanged cells left as they are): {e}")

    def fill(first_row: int, end_row: int, colour: Dict[str, float]) -> Dict[str, Any]:
        return {"repeatCell": {
            "range": {"sheetId": ws.id, "startRowIndex": first_row - 1, "endRowIndex": end_row,
                      "startColumnIndex": col - 1, "endColumnIndex": col},
            "cell": {"userEnteredFormat": {"backgroundColor": colour}},
            "fields": "userEnteredFormat.backgroundColor",
        }}

    # neighbouring rows with the same colour share one request
    requests = []
    run_start = prev_row = run_colour = None
    for r in sorted(colours):
        if run_colour is not None and r == prev_row + 1 and colours[r] == run_colour:
            prev_row = r
            continue
        if run_colour is not None:
            requests.append(fill(run_start, prev_row, run_colour))
        run_start = prev_row = r
        run_colour = colours[r]
    if run_colour is not None:
        requests.append(fill(run_start, prev_row, run_colour))
    if requests:
        sh.batch_update({"requests": requests})
    logger.info(f"Rating change vs column {prev_letter}: {up} increased (light green), {down} dropped (light red)")


def run(spreadsheet_id: str, sheet_name: str, target: date, test_limit: Optional[int] = None):
    import gspread
    from gsheet_engine import get_gspread_client
    from config import find_matching_sheet_name

    client = get_gspread_client()
    if not client:
        raise RuntimeError("Google Service Account credentials missing (GDRIVE_SERVICE_ACCOUNT_JSON).")

    sh = client.open_by_key(spreadsheet_id)
    titles = [w.title for w in sh.worksheets()]
    matched = find_matching_sheet_name(sheet_name, titles)
    if matched not in titles:
        raise RuntimeError(f"Tab '{sheet_name}' not found in the spreadsheet. Tabs: {titles}")
    ws = sh.worksheet(matched)
    logger.info(f"Connected to tab '{ws.title}' ({ws.row_count} rows, {ws.col_count} cols)")

    # ---- product rows (column D holds the link; A/B are merged so they are not needed)
    info = ws.get(f"A{FIRST_DATA_ROW}:{col_to_letter(COL_LINK)}{ws.row_count}")
    products = []
    for offset, row_vals in enumerate(info):
        link = row_vals[COL_LINK - 1].strip() if len(row_vals) >= COL_LINK else ""
        if not link.startswith("http"):
            continue
        model = row_vals[2].strip() if len(row_vals) >= 3 else ""
        products.append({"row": FIRST_DATA_ROW + offset, "link": link, "model": model})
    if not products:
        logger.warning("No product links found in column D. Nothing to do.")
        return
    last_row = max(p["row"] for p in products)
    if test_limit:
        products = products[:test_limit]
    logger.info(f"{len(products)} products to check (rows {products[0]['row']} to {products[-1]['row']})")

    # ---- today's column
    header_rows = ws.get(f"{HEADER_ROW}:{HEADER_ROW}", value_render_option="UNFORMATTED_VALUE")
    header = header_rows[0] if header_rows else []
    col, is_new = find_date_column(header, target)
    letter = col_to_letter(col)
    if is_new:
        logger.info(f"Adding new date column {letter} for {target.isoformat()}")
        if col > ws.col_count:
            ws.add_cols(col - ws.col_count + 10)
        if col - 1 >= FIRST_DATE_COL:
            # same look as the day before: copy only the FORMAT of the previous column
            try:
                sh.batch_update({"requests": [{"copyPaste": {
                    "source": {"sheetId": ws.id, "startRowIndex": 0, "endRowIndex": last_row,
                               "startColumnIndex": col - 2, "endColumnIndex": col - 1},
                    "destination": {"sheetId": ws.id, "startRowIndex": 0, "endRowIndex": last_row,
                                    "startColumnIndex": col - 1, "endColumnIndex": col},
                    "pasteType": "PASTE_FORMAT", "pasteOrientation": "NORMAL"}}]})
            except Exception as e:
                logger.warning(f"Note on copying the column format: {e}")
        ws.update_cells([gspread.Cell(HEADER_ROW, col, target.strftime("%Y-%m-%d"))],
                        value_input_option="USER_ENTERED")
    else:
        logger.info(f"Found existing date column {letter} for {target.isoformat()} - updating it")

    # ---- fetch + write
    t0 = time.time()
    ratings = fetch_ratings(products)
    cells = [gspread.Cell(p["row"], col, ratings[p["row"]]) for p in products if p["row"] in ratings]
    if cells:
        ws.update_cells(cells, value_input_option="USER_ENTERED")
    try:
        highlight_rating_changes(sh, ws, col, ratings, last_row)
    except Exception as e:
        logger.warning(f"Note on rating-change highlighting: {e}")
    missing = [p for p in products if p["row"] not in ratings]
    logger.info("=" * 60)
    logger.info(f"  RATING TRACKER COMPLETE - column {letter} ({target.isoformat()})")
    logger.info(f"  Ratings written: {len(cells)} | not available: {len(missing)} | {time.time() - t0:.1f}s")
    for p in missing:
        logger.info(f"    no rating: row {p['row']} | {p['model']} | {p['link'][:70]}")
    logger.info("=" * 60)


def main():
    ap = argparse.ArgumentParser(description="Daily rating tracker")
    ap.add_argument("--gsheet", default=None, help="Spreadsheet ID")
    ap.add_argument("--sheet", default=None, help="Tab name (default 'Ratings Tracker')")
    ap.add_argument("--date", default=None, help="Date to fill, YYYY-MM-DD (default: today, IST)")
    ap.add_argument("--test", type=int, default=None, help="Only the first N products")
    a = ap.parse_args()

    spreadsheet_id = (a.gsheet or os.getenv("RATING_SPREADSHEET_ID") or os.getenv("GSHEET_SPREADSHEET_ID") or "").strip()
    if not spreadsheet_id:
        sys.exit("No spreadsheet ID. Set RATING_SPREADSHEET_ID (or GSHEET_SPREADSHEET_ID) or pass --gsheet.")
    sheet_name = (a.sheet or os.getenv("RATING_SHEET") or DEFAULT_SHEET).strip()

    raw_date = (a.date or os.getenv("CUSTOM_DATE") or "").strip().strip("'\"")
    target = today_ist()
    if raw_date:
        parsed = header_to_date(raw_date)
        if not parsed:
            sys.exit(f"Could not read the date '{raw_date}'. Use YYYY-MM-DD.")
        target = parsed

    test_limit = a.test
    if test_limit is None and (os.getenv("TEST_LIMIT") or "").strip():
        try:
            test_limit = int(os.getenv("TEST_LIMIT").strip())
        except ValueError:
            test_limit = None

    logger.info("=" * 60)
    logger.info("  RATING TRACKER")
    logger.info(f"  Tab: {sheet_name} | Date: {target.isoformat()}" + (f" | Test limit: {test_limit}" if test_limit else ""))
    logger.info("=" * 60)
    run(spreadsheet_id, sheet_name, target, test_limit)


if __name__ == "__main__":
    main()

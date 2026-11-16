"""
Turns the Amazon Ads "Products" export into Units Sold / Clicks per model and
writes them into the online Google Sheet (tab "Ads_Data").

This replaces the manual Excel step:
    AZ_data_source (pasted export)  ->  AZ_data (VLOOKUP)  ->  copy into Google Sheet

  Units Sold = "Orders" column of the export   (VLOOKUP ...,13,0)
  Clicks     = "Clicks" column of the export   (VLOOKUP ...,9,0)
  lookup key = "<ASIN>-<ASIN>" in the "Products" column, first match wins
"""
import base64
import csv
import json
import os
import re
from datetime import date, datetime
from pathlib import Path

from gspread.utils import rowcol_to_a1

HERE = Path(__file__).resolve().parent
QUIET = os.environ.get("ADS_QUIET", "") == "1"   # public CI logs: no ads numbers, no sheet name
DATE_FORMATS =("%d-%b-%y", "%d-%b-%Y", "%d %b %Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y")


# ----------------------------------------------------------------- export parsing
def _num(v):
    if v is None or v == "":
        return 0
    if isinstance(v, (int, float)):
        return v
    s = re.sub(r"[^\d.\-]", "", str(v))
    if s in ("", "-", "."):
        return 0
    f = float(s)
    return int(f) if f.is_integer() else f


def _read_rows(path):
    if str(path).lower().endswith((".xlsx", ".xlsm")):
        import openpyxl
        ws = openpyxl.load_workbook(path, read_only=True, data_only=True).worksheets[0]
        return [list(r) for r in ws.iter_rows(values_only=True)]
    with open(path, newline="", encoding="utf-8-sig") as f:
        sample = f.read(4096)
        f.seek(0)
        delim = "\t" if sample.count("\t") > sample.count(",") else ","
        return list(csv.reader(f, delimiter=delim))


def parse_export(path):
    """-> {"B0XXXXXXXX-B0XXXXXXXX": {"orders": n, "clicks": n}, ...}"""
    rows = _read_rows(path)
    hdr_i = next((i for i, r in enumerate(rows)
                  if any(str(c or "").strip().lower() == "clicks" for c in r)), None)
    if hdr_i is None:
        raise ValueError(f"No 'Clicks' header found in {path} - is this the Products export?")
    hdr = [str(c or "").strip().lower() for c in rows[hdr_i]]

    def col(*names):
        for n in names:
            for i, h in enumerate(hdr):
                if h == n or h.startswith(n + "("):
                    return i
        raise ValueError(f"Column {names} not found in export. Headers: {rows[hdr_i]}")

    c_prod, c_clicks, c_orders = col("products", "product"), col("clicks"), col("orders")
    out = {}
    for r in rows[hdr_i + 1:]:
        if len(r) <= max(c_prod, c_clicks, c_orders):
            continue
        key = str(r[c_prod] or "").strip()
        if key and key not in out:               # VLOOKUP: first match wins
            out[key] = {"orders": _num(r[c_orders]), "clicks": _num(r[c_clicks])}
    if not out:
        raise ValueError(f"Export {path} has a header but no product rows")
    return out


def lookup(data, asin):
    return data.get(f"{asin}-{asin}", {"orders": 0, "clicks": 0})


# ----------------------------------------------------------------- sheet helpers
def norm(s):
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()


def parse_date(v):
    s = str(v or "").strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def date_label(d: date):
    return f"{d.day}-{d:%b-%y}"          # 13-Nov-26, 2-Nov-26


def _header_text(d: date):
    return "'" + date_label(d)           # leading ' = stored as plain text, like the hand-typed headers


def _body(col_a, title_row):
    """(first model row, Total row) of the section that starts at title_row."""
    r = title_row + 2
    while r <= len(col_a) and norm(col_a[r - 1]) != "total":
        r += 1
    return (title_row + 2, r) if r <= len(col_a) else None


def format_counts(ws, col, bodies):
    """Units Sold / Clicks cells are whole numbers (a copied block may carry a % format)."""
    reqs = [{"repeatCell": {
        "range": {"sheetId": ws.id, "startRowIndex": first - 1, "endRowIndex": total,
                  "startColumnIndex": col - 1, "endColumnIndex": col + 1},
        "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": "0"}}},
        "fields": "userEnteredFormat.numberFormat"}} for first, total in bodies]
    if reqs:
        ws.spreadsheet.batch_update({"requests": reqs})


def open_worksheet(cfg):
    import gspread
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if raw:
        if not raw.startswith("{"):
            raw = base64.b64decode(raw).decode("utf-8")
        gc = gspread.service_account_from_dict(json.loads(raw))
    elif (HERE / "service_account.json").exists():
        gc = gspread.service_account(filename=str(HERE / "service_account.json"))
    else:
        raise RuntimeError("Set GOOGLE_SERVICE_ACCOUNT_JSON (or put service_account.json next to run.py)")
    # The sheet id is never committed (public repo): env SHEET_ID on GitHub, sheet_id.txt locally.
    sheet = os.environ.get("SHEET_ID", "").strip()
    if not sheet and (HERE / "sheet_id.txt").exists():
        sheet = (HERE / "sheet_id.txt").read_text(encoding="utf-8").strip()
    if not sheet:
        raise RuntimeError("Set SHEET_ID (or put the Google Sheet id / URL in sheet_id.txt)")
    sh = gc.open_by_url(sheet) if sheet.startswith("http") else gc.open_by_key(sheet)
    try:
        ws = sh.get_worksheet_by_id(cfg["sheet_gid"]) if cfg.get("sheet_gid") is not None \
            else sh.worksheet(cfg["sheet_tab"])
    except gspread.WorksheetNotFound:
        raise RuntimeError(f"Tab '{cfg['sheet_tab']}' is not in the spreadsheet given by SHEET_ID - "
                           "set the ADS_SPREADSHEET_ID secret to the spreadsheet that holds it")
    if not QUIET:
        print(f"Google Sheet: '{sh.title}' / tab '{ws.title}'")
    return ws


def _add_date_columns(ws, title_rows, last_col, last_date, target, log):
    """Copy the last day's 3 columns to the right and turn the copy into an empty block for `target`."""
    new_col = last_col + 3
    if ws.col_count < new_col + 2:
        ws.add_cols(new_col + 2 - ws.col_count)
    rng = lambda c: {"sheetId": ws.id, "startColumnIndex": c - 1, "endColumnIndex": c + 2}
    ws.spreadsheet.batch_update({"requests": [{"copyPaste": {
        "source": rng(last_col), "destination": rng(new_col),
        "pasteType": "PASTE_NORMAL", "pasteOrientation": "NORMAL"}}]})

    n = ws.row_count
    a1 = f"{rowcol_to_a1(1, new_col)}:{rowcol_to_a1(n, new_col + 2)}"
    block = ws.get(a1, value_render_option="FORMULA")
    fixes = []
    for r, row in enumerate(block, start=1):
        for off, v in enumerate(row):
            if v == "" or v is None:
                continue
            if r in title_rows and off == 0:
                continue                                   # date header, rewritten below
            if isinstance(v, str) and v.startswith("="):
                continue                                   # formulas (CR, Total) stay
            if isinstance(v, str) and not _is_number(v) and parse_date(v) is None:
                continue                                   # text labels (Units Sold / Clicks / CR) stay
            fixes.append({"range": rowcol_to_a1(r, new_col + off), "values": [[""]]})  # yesterday's numbers
    for r in title_rows:                                   # every report section gets the new date
        fixes.append({"range": rowcol_to_a1(r, new_col), "values": [[_header_text(target)]]})
    ws.batch_update(fixes, value_input_option="USER_ENTERED")
    col_a = ws.col_values(1)
    format_counts(ws, new_col, [b for b in (_body(col_a, t) for t in title_rows) if b])
    log(f"Added columns {rowcol_to_a1(1, new_col)[:-1]}..{rowcol_to_a1(1, new_col + 2)[:-1]} for {date_label(target)}")
    return new_col


def _is_number(s):
    try:
        float(str(s).replace(",", "").replace("%", ""))
        return True
    except ValueError:
        return False


# ----------------------------------------------------------------- main entry
def update_sheet(ws, cfg, data, target: date, dry_run=False, log=print):
    col_a = ws.col_values(1)
    title_rows = [i for i, v in enumerate(col_a, start=1) if "ads sales report" in norm(v)]
    wanted = {norm(s["title"]): s for s in cfg["sections"]}
    sections = [(r, wanted[norm(col_a[r - 1])]) for r in title_rows if norm(col_a[r - 1]) in wanted]
    missing = set(wanted) - {norm(s["title"]) for _, s in sections}
    if missing:
        raise RuntimeError(f"Section title(s) not found in column A: {sorted(missing)}")

    # ---- find (or create) the 3 columns of the target date
    header = ws.row_values(sections[0][0])
    dated = [(i, parse_date(v)) for i, v in enumerate(header, start=1) if i > 1 and parse_date(v)]
    col = next((i for i, d in dated if d == target), None)
    if col is None:
        if not dated:
            raise RuntimeError("No date headers found in the Amazon section header row")
        last_col, last_date = max(dated, key=lambda x: x[0])
        if target < last_date:
            raise RuntimeError(f"{date_label(target)} is older than the last column "
                               f"({date_label(last_date)}) and has no column - not adding it out of order")
        if dry_run:
            col = last_col + 3
            log(f"[dry-run] would add 3 new columns at {rowcol_to_a1(1, col)[:-1]} for {date_label(target)}")
        else:
            col = _add_date_columns(ws, title_rows, last_col, last_date, target, log)
    else:
        log(f"Found {date_label(target)} at column {rowcol_to_a1(1, col)[:-1]}")

    # ---- build the writes for both Amazon sections
    updates, summary = [], []
    for title_row, sec in sections:
        models = {norm(k): v for k, v in sec["models"].items()}
        first = title_row + 2
        r, seen = first, set()
        while r <= len(col_a) and norm(col_a[r - 1]) not in ("total", ""):
            name = norm(col_a[r - 1])
            u, c, cr = (rowcol_to_a1(r, col + k) for k in range(3))
            if name in models:
                d = lookup(data, models[name])
                updates += [{"range": f"{u}:{c}", "values": [[d["orders"], d["clicks"]]]},
                            {"range": cr, "values": [[f"={u}/{c}"]]}]
                summary.append((sec["title"], col_a[r - 1], d["orders"], d["clicks"]))
                seen.add(name)
            else:
                log(f"WARNING: row {r} '{col_a[r - 1]}' is not in config.json - left untouched")
            r += 1
        if r > len(col_a) or norm(col_a[r - 1]) != "total":
            raise RuntimeError(f"No 'Total' row found under '{sec['title']}'")
        for k in (0, 1):
            top, bot = rowcol_to_a1(first, col + k), rowcol_to_a1(r - 1, col + k)
            updates.append({"range": rowcol_to_a1(r, col + k), "values": [[f"=SUM({top}:{bot})"]]})
        updates.append({"range": rowcol_to_a1(r, col + 2),
                        "values": [[f"={rowcol_to_a1(r, col)}/{rowcol_to_a1(r, col + 1)}"]]})
        for m in set(models) - seen:
            log(f"WARNING: model '{m}' from config.json has no row under '{sec['title']}'")

    for title, model, units, clicks in ([] if QUIET else summary):
        log(f"  {title[:15]:15}  {model:14} units={units:<4} clicks={clicks}")
    if dry_run:
        log(f"[dry-run] {len(updates)} ranges NOT written")
    else:
        ws.batch_update(updates, value_input_option="USER_ENTERED")
        format_counts(ws, col, [b for b in (_body(col_a, t) for t, _ in sections) if b])
        log(f"Wrote {len(summary)} model rows for {date_label(target)}")
    return summary

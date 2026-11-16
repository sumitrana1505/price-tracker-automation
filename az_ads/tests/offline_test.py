"""
Offline check of the sheet logic - no Google / Amazon access needed.

Uses the two local workbooks as stand-ins:
  Ads_data_file.xlsx  sheet AZ_data_source  = an Amazon export (12-Nov-26)
  Ads_data.xlsx       sheet Ads_Data        = a download of the online Google Sheet

  python tests/offline_test.py "<folder with the two xlsx files>"
"""
import csv
import json
import sys
import tempfile
from datetime import date
from pathlib import Path

import openpyxl
from gspread.utils import a1_range_to_grid_range, a1_to_rowcol, rowcol_to_a1

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import sheet_update  # noqa: E402


class FakeSheet:
    """Just enough of gspread.Worksheet, backed by a dict {(row, col): value}."""
    id = 0

    def __init__(self, xlsx, tab):
        ws = openpyxl.load_workbook(xlsx)[tab]
        self.g = {(c.row, c.column): c.value for row in ws.iter_rows() for c in row
                  if c.value is not None and not isinstance(c.value, (dict, list))}
        self.row_count, self.col_count = ws.max_row, ws.max_column
        self.spreadsheet, self.added = self, 0

    def col_values(self, c):
        n = max(r for r, cc in self.g if cc == c)
        return [str(self.g.get((r, c), "")) for r in range(1, n + 1)]

    def row_values(self, r):
        n = max(c for rr, c in self.g if rr == r)
        return [str(self.g.get((r, c), "")) for c in range(1, n + 1)]

    def add_cols(self, n):
        self.col_count += n
        self.added += n

    def get(self, a1, value_render_option=None):
        gr = a1_range_to_grid_range(a1)
        return [[self.g.get((r, c), "") for c in range(gr["startColumnIndex"] + 1, gr["endColumnIndex"] + 1)]
                for r in range(gr["startRowIndex"] + 1, gr["endRowIndex"] + 1)]

    def batch_update(self, body, value_input_option=None):
        if isinstance(body, dict):                       # spreadsheet.batch_update -> copyPaste
            if "copyPaste" not in body["requests"][0]:
                return                                   # formatting requests: nothing to simulate
            cp = body["requests"][0]["copyPaste"]
            s, d = cp["source"]["startColumnIndex"] + 1, cp["destination"]["startColumnIndex"] + 1
            for (r, c), v in list(self.g.items()):
                if s <= c < s + 3:
                    self.g[(r, c - s + d)] = v
            return
        for u in body:                                   # worksheet.batch_update -> values
            r0, c0 = a1_to_rowcol(u["range"].split(":")[0])
            for i, row in enumerate(u["values"]):
                for j, v in enumerate(row):
                    if v == "":
                        self.g.pop((r0 + i, c0 + j), None)
                    else:                                # a leading ' means "plain text" in Sheets
                        self.g[(r0 + i, c0 + j)] = v[1:] if isinstance(v, str) and v.startswith("'") else v

    def cell(self, a1):
        return self.g.get(a1_to_rowcol(a1))


def main(folder):
    folder = Path(folder)
    cfg = json.loads((Path(__file__).resolve().parent.parent / "config.json").read_text(encoding="utf-8"))

    # AZ_data_source -> csv, like a fresh Amazon export
    src = openpyxl.load_workbook(folder / "Ads_data_file.xlsx", data_only=True)["AZ_data_source"]
    tmp = Path(tempfile.mkdtemp()) / "export.csv"
    with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
        csv.writer(f).writerows(src.iter_rows(values_only=True))
    data = sheet_update.parse_export(tmp)
    assert sheet_update.lookup(data, "B0FDQTLSFT") == {"orders": 7, "clicks": 955}, data

    # 1) date column already exists (12-Nov-26 = XA) -> must reproduce what was typed by hand
    ws = FakeSheet(folder / "Ads_data.xlsx", "Ads_Data")
    before = dict(ws.g)
    sheet_update.update_sheet(ws, cfg, data, date(2026, 11, 12), log=lambda *_: None)
    bad = []
    for r in list(range(33, 44)) + list(range(48, 55)):
        for col in ("XA", "XB"):
            old, new = before.get(a1_to_rowcol(f"{col}{r}")), ws.cell(f"{col}{r}")
            if (old or 0) != new:
                bad.append((f"{col}{r}", old, new))
    assert not bad, f"differs from the manual 12-Nov values: {bad}"
    assert ws.cell("XA44") == "=SUM(XA33:XA43)" and ws.cell("XC55") == "=XA55/XB55"
    changed = {rowcol_to_a1(*k) for k in ws.g if ws.g[k] != before.get(k)}
    assert all(c.startswith(("XA", "XB", "XC", "AO", "AP")) for c in changed), changed
    # Nov MTD Actual: 1..13-Nov (VT..XD); clicks use the Clicks column of every day (VX, not VW)
    assert ws.cell("AO33") == "=SUM(VT33,VW33,VZ33,WC33,WF33,WI33,WL33,WO33,WR33,WU33,WX33,XA33,XD33)", ws.cell("AO33")
    assert ws.cell("AP33") == "=SUM(VU33,VX33,WA33,WD33,WG33,WJ33,WM33,WP33,WS33,WV33,WY33,XB33,XE33)", ws.cell("AP33")
    assert ws.cell("AP55") == "=SUM(AP48:AP54)" and ws.cell("AO44") == "=SUM(AO33:AO43)"
    assert ws.cell("AO3") == before[(3, 41)]                        # Flipkart MTD untouched
    print("PASS 1: existing 12-Nov column reproduced exactly; nothing else touched")

    # 2) date column missing (15-Nov-26) -> 3 new columns XG:XI in all four sections
    ws = FakeSheet(folder / "Ads_data.xlsx", "Ads_Data")
    sheet_update.update_sheet(ws, cfg, data, date(2026, 11, 15), log=lambda *_: None)
    assert ws.added == 3
    for r in (1, 16, 31, 46):
        assert ws.cell(f"XG{r}") == "15-Nov-26", (r, ws.cell(f"XG{r}"))
        assert ws.cell(f"XG{r + 1}") == "Units Sold" and ws.cell(f"XI{r + 1}") == "CR"
    assert ws.cell("XG36") == 7 and ws.cell("XH36") == 955 and ws.cell("XI36") == "=XG36/XH36"
    assert ws.cell("XG3") is None and ws.cell("XH18") is None      # Flipkart rows left empty
    assert ws.cell("XD31") == "13-Nov-26"                           # previous day untouched
    assert ws.cell("AO36").endswith(",XA36,XD36,XG36)") and ws.cell("AP36").endswith(",XB36,XE36,XH36)")
    print("PASS 2: missing date -> new XG:XI block with headers, formulas and Amazon numbers")


if __name__ == "__main__":
    main(sys.argv[1])

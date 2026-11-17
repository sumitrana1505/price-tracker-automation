"""
Turns Flipkart Ads "Consolidated FSN Report" exports into Units Sold / Clicks per FSN.

This replaces the manual Excel step:
    FK_data_source (pasted exports of both accounts)  ->  FK_data (SUMIFS)  ->  Google Sheet

  Units Sold = SUM of "Units Sold (Direct)"  for the FSN     (SUMIFS FK_data_source!L)
  Clicks     = SUM of "Clicks"               for the FSN     (SUMIFS FK_data_source!H)
  key        = "Advertised FSN ID"                            (FK_data_source!E)
"""
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "az_ads"))
from sheet_update import _num, _read_rows  # noqa: E402


def _cell_date(v):
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return datetime.strptime(str(v or "").strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_report(path, expect_day: date = None):
    """-> {"TVSH...": {"orders": n, "clicks": n}, ...}   (rows of the same FSN are added up)"""
    rows = _read_rows(path)
    low = lambda r: [str(c or "").strip().lower() for c in r]
    hdr_i = next((i for i, r in enumerate(rows) if "advertised fsn id" in low(r)), None)
    if hdr_i is None:
        raise ValueError(f"No 'Advertised FSN ID' header in {Path(path).name} - "
                         "is this the Consolidated FSN Report?")
    hdr = low(rows[hdr_i])

    if expect_day is not None:              # the file itself says which period it covers
        meta = {l[0]: _cell_date(r[1]) for r in rows[:hdr_i] if len(r) > 1 and (l := low(r))}
        start, end = meta.get("start time"), meta.get("end time")
        if start != expect_day or end != expect_day:
            raise ValueError(f"{Path(path).name} covers {start} .. {end}, but {expect_day} was "
                             "expected - not using a report of the wrong day")

    def col(name):
        if name not in hdr:
            raise ValueError(f"Column '{name}' not found in export. Headers: {rows[hdr_i]}")
        return hdr.index(name)

    c_fsn, c_clicks, c_units = col("advertised fsn id"), col("clicks"), col("units sold (direct)")
    out = {}
    for r in rows[hdr_i + 1:]:
        if len(r) <= max(c_fsn, c_clicks, c_units):
            continue
        fsn = str(r[c_fsn] or "").strip()
        if not fsn:
            continue
        d = out.setdefault(fsn, {"orders": 0, "clicks": 0})
        d["orders"] += _num(r[c_units])
        d["clicks"] += _num(r[c_clicks])
    return out


def merge(reports):
    """Both accounts pasted below each other = one table; SUMIFS adds them up."""
    out = {}
    for rep in reports:
        for fsn, d in rep.items():
            t = out.setdefault(fsn, {"orders": 0, "clicks": 0})
            t["orders"] += d["orders"]
            t["clicks"] += d["clicks"]
    return out

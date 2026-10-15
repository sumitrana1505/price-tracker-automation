"""
Direct Google Sheets Brain & Synchronization Engine
====================================================
Uses gspread to directly read competitor links from your Google Sheet,
fetch live price/offer/rating data from Flipkart & Amazon,
and write today's 6-column block directly to the live Google Sheet in real time.
"""

import json
import logging
import os
import sys
import time
from datetime import datetime
from typing import Optional, List, Dict, Any

# Windows console UTF-8 output configuration
if sys.platform == "win32":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if sys.stderr and hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import gspread
from google.oauth2 import service_account

from config import (
    SHEET_NAME,
    FIRST_DATA_ROW,
    LAST_DATA_ROW,
    COL_CHANNEL,
    COL_MODEL,
    COL_LINK,
    BLOCK_SIZE,
    SUB_HEADERS,
    format_today_header,
)
from scrapers import FlipkartScraper, AmazonScraper, ProductScrapedData

logger = logging.getLogger("GoogleSheetsBrain")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)]
)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def col_to_letter(col_idx: int) -> str:
    """Converts 1-based column index to A1 notation letter (e.g., 1->A, 27->AA)"""
    result = ""
    while col_idx > 0:
        col_idx, remainder = divmod(col_idx - 1, 26)
        result = chr(65 + remainder) + result
    return result


def get_gspread_client() -> Optional[gspread.Client]:
    """Authenticates gspread client using Service Account JSON (Env var or local file)"""
    env_json = os.getenv("GDRIVE_SERVICE_ACCOUNT_JSON")
    if env_json:
        try:
            info = json.loads(env_json)
            creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
            return gspread.authorize(creds)
        except Exception as e:
            logger.error(f"Failed to parse credentials from GDRIVE_SERVICE_ACCOUNT_JSON: {e}")

    file_path = os.getenv("GDRIVE_CREDENTIALS_FILE", "service_account.json")
    if os.path.exists(file_path):
        try:
            creds = service_account.Credentials.from_service_account_file(file_path, scopes=SCOPES)
            return gspread.authorize(creds)
        except Exception as e:
            logger.error(f"Failed to load credentials from file {file_path}: {e}")

    logger.warning("No Google Service Account credentials found.")
    return None


class GoogleSheetPriceTrackerBrain:
    """Automates daily price fetching directly inside a Google Sheet"""

    def __init__(self, spreadsheet_id: str, date_header: Optional[str] = None):
        self.spreadsheet_id = spreadsheet_id
        self.date_header = date_header or format_today_header()
        self.client = get_gspread_client()
        self.fk_scraper = FlipkartScraper()
        self.amz_scraper = AmazonScraper()

    def run(self, test_limit: Optional[int] = None):
        if not self.client:
            raise RuntimeError(
                "Google Service Account credentials missing. Please set GDRIVE_SERVICE_ACCOUNT_JSON "
                "or place 'service_account.json' in this folder."
            )

        logger.info(f"Opening Google Sheet with ID: {self.spreadsheet_id}")
        sh = self.client.open_by_key(self.spreadsheet_id)
        try:
            ws = sh.worksheet(SHEET_NAME)
        except Exception:
            # Fallback to first worksheet if sheet name differs slightly
            ws = sh.get_worksheet(0)
            logger.warning(f"Sheet '{SHEET_NAME}' not found; using '{ws.title}' instead.")

        logger.info(f"Connected to worksheet '{ws.title}' ({ws.row_count} rows, {ws.col_count} cols)")

        # Read row 1 and row 2 headers to find or create the date block
        row1 = ws.row_values(1)
        row2 = ws.row_values(2)

        # 1. Search for existing date block in row 1
        start_col = None
        for idx, val in enumerate(row1):
            if str(val).strip().lower() == self.date_header.strip().lower():
                start_col = idx + 1
                logger.info(f"Found existing date block for '{self.date_header}' at Col {start_col} ({col_to_letter(start_col)})")
                break

        # 2. If not found, find last column in row 2 with data
        if start_col is None:
            last_used_col = 20
            for idx, val in enumerate(row2):
                if val is not None and str(val).strip() != "":
                    last_used_col = idx + 1
            start_col = last_used_col + 1
            logger.info(f"Creating NEW date block for '{self.date_header}' starting at Col {start_col} ({col_to_letter(start_col)})")

            # Check if worksheet needs more columns
            needed_cols = start_col + BLOCK_SIZE - 1
            if needed_cols > ws.col_count:
                ws.add_cols(needed_cols - ws.col_count + 10)

            # Write Date in Row 1 and Sub-headers in Row 2
            ws.update_cell(1, start_col, self.date_header)
            sub_header_cells = [
                gspread.Cell(2, start_col + i, sub)
                for i, sub in enumerate(SUB_HEADERS)
            ]
            ws.update_cells(sub_header_cells, value_input_option="USER_ENTERED")

        col_price = start_col
        col_bank = start_col + 1
        col_coupon = start_col + 2
        col_nep = start_col + 3
        col_rating = start_col + 4
        col_rc = start_col + 5

        col_price_letter = col_to_letter(col_price)
        col_bank_letter = col_to_letter(col_bank)
        col_coupon_letter = col_to_letter(col_coupon)

        # Read product rows (channels, models, links)
        logger.info(f"Reading product rows from {FIRST_DATA_ROW} to {LAST_DATA_ROW}...")
        all_rows = ws.get_all_values()

        cells_to_update = []
        processed_count = 0
        success_count = 0
        fail_count = 0
        t0 = time.time()

        for r in range(FIRST_DATA_ROW, min(LAST_DATA_ROW + 1, len(all_rows) + 1)):
            row_data = all_rows[r - 1] if (r - 1) < len(all_rows) else []
            channel = row_data[COL_CHANNEL - 1].strip() if len(row_data) >= COL_CHANNEL else ""
            model = row_data[COL_MODEL - 1].strip() if len(row_data) >= COL_MODEL else ""
            link = row_data[COL_LINK - 1].strip() if len(row_data) >= COL_LINK else ""

            if not channel and not model and not link:
                continue

            nep_formula = f"=IFERROR({col_price_letter}{r}-{col_bank_letter}{r}-{col_coupon_letter}{r},0)"

            if not link or not link.startswith("http"):
                cells_to_update.extend([
                    gspread.Cell(r, col_price, ""),
                    gspread.Cell(r, col_bank, ""),
                    gspread.Cell(r, col_coupon, 0),
                    gspread.Cell(r, col_nep, nep_formula),
                    gspread.Cell(r, col_rating, ""),
                    gspread.Cell(r, col_rc, ""),
                ])
                continue

            if test_limit and processed_count >= test_limit:
                logger.info(f"Reached test limit ({test_limit} products). Stopping scrape.")
                break

            processed_count += 1
            logger.info(f"[{processed_count}] Row {r} | {channel.capitalize()} | Model: {model}")

            scraped = None
            try:
                ch_lower = channel.lower()
                if "flipkart" in ch_lower or "flipkart" in link:
                    scraped = self.fk_scraper.scrape(link, model=model)
                elif "amazon" in ch_lower or "amazon" in link:
                    scraped = self.amz_scraper.scrape(link, model=model)
            except Exception as e:
                logger.error(f"  Scrape error on row {r}: {e}")

            if scraped and scraped.price:
                cells_to_update.extend([
                    gspread.Cell(r, col_price, scraped.price),
                    gspread.Cell(r, col_bank, scraped.bank_offer if scraped.bank_offer else ""),
                    gspread.Cell(r, col_coupon, scraped.coupon or 0),
                    gspread.Cell(r, col_nep, nep_formula),
                    gspread.Cell(r, col_rating, scraped.rating if scraped.rating else ""),
                    gspread.Cell(r, col_rc, scraped.rating_count if scraped.rating_count else ""),
                ])
                nep_val = scraped.price - (scraped.bank_offer or 0) - (scraped.coupon or 0)
                logger.info(
                    f"  [SUCCESS] Price: Rs.{scraped.price:,} | "
                    f"Bank Offer: Rs.{scraped.bank_offer or 0:,} | "
                    f"Coupon: Rs.{scraped.coupon or 0:,} | "
                    f"NEP: Rs.{nep_val:,} | "
                    f"Rating: {scraped.rating} ({scraped.rating_count or 0})"
                )
                success_count += 1
            else:
                cells_to_update.extend([
                    gspread.Cell(r, col_price, ""),
                    gspread.Cell(r, col_bank, ""),
                    gspread.Cell(r, col_coupon, 0),
                    gspread.Cell(r, col_nep, nep_formula),
                    gspread.Cell(r, col_rating, scraped.rating if scraped else ""),
                    gspread.Cell(r, col_rc, scraped.rating_count if scraped else ""),
                ])
                fail_count += 1

            # Batch write to Google Sheet every 10 products
            if len(cells_to_update) >= 30:
                try:
                    ws.update_cells(cells_to_update, value_input_option="USER_ENTERED")
                    cells_to_update = []
                    logger.info("  [SYNC] Batch saved to Google Sheet.")
                except Exception as e:
                    logger.warning(f"  Batch save warning: {e}")

        # Final batch update
        if cells_to_update:
            try:
                ws.update_cells(cells_to_update, value_input_option="USER_ENTERED")
                logger.info("  [SYNC] Final batch saved to Google Sheet.")
            except Exception as e:
                logger.error(f"  Final save error: {e}")

        elapsed = time.time() - t0
        logger.info("=" * 60)
        logger.info("  GOOGLE SHEET CRAWL COMPLETE")
        logger.info(f"  Total Processed: {processed_count}")
        logger.info(f"  Successful: {success_count} | OOS/Failed: {fail_count}")
        logger.info(f"  Elapsed: {elapsed:.1f}s ({elapsed/60:.1f} mins)")
        logger.info("=" * 60)

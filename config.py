# Configuration settings for Daily Price Tracker Cloud Brain

import os
from datetime import datetime

# ==========================================
# FILE & SHEET CONFIGURATION
# ==========================================
# Preferred primary workbook name
DEFAULT_WORKBOOK = "PriceTracker (6).xlsx"
FALLBACK_WORKBOOK = "PriceTracker.xlsx"

SHEET_NAME = "Competitor price monitoring"

# Data row boundaries in "Competitor price monitoring"
FIRST_DATA_ROW = 3
LAST_DATA_ROW = 249

# Fixed input columns (1-based index)
COL_SERIES_NUM = 1      # Col A: Priority / Index
COL_TARGET_MODEL = 2    # Col B: Target Model
COL_CHANNEL = 3         # Col C: Channel (Flipkart / Amazon)
COL_SIZE = 4            # Col D: Size
COL_BRAND = 5           # Col E: Brand
COL_SERIES = 6          # Col F: Series
COL_MODEL = 7           # Col G: Model
COL_DISPLAY_TECH = 8    # Col H: Display Tech
COL_LINK = 9            # Col I: Product Link

# Daily block layout (6 columns per date)
BLOCK_SIZE = 6
SUB_HEADERS = [
    "Price",
    "Cash Back / Bank_offer",
    "Coupon",
    "NEP",
    "\nRating",
    "Rating Count"
]

# Scraping settings
DEFAULT_PINCODE = "110001"       # Delhi
FALLBACK_PINCODE = "700032"      # Kolkata
REQUEST_TIMEOUT = 25
DELAY_MIN = 1.5
DELAY_MAX = 3.5
MAX_RETRIES = 3

# Google Drive / Sheets settings
# Can be provided via environment variables or service_account.json
GDRIVE_CREDENTIALS_FILE = os.getenv("GDRIVE_CREDENTIALS_FILE", "service_account.json")
GDRIVE_FILE_ID = os.getenv("GDRIVE_FILE_ID", "")
GSHEET_SPREADSHEET_ID = os.getenv("GSHEET_SPREADSHEET_ID", "")


def format_today_header(target_dt: datetime = None) -> str:
    """
    Formats a date into the style used in the tracker sheet.
    Examples: '16th - Oct', '17th - Oct', '1st - Nov', etc.
    """
    if target_dt is None:
        target_dt = datetime.now()
    
    day = target_dt.day
    if 11 <= (day % 100) <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    
    month_str = target_dt.strftime("%b")
    return f"{day}{suffix} - {month_str}"

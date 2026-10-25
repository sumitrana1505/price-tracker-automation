# Configuration settings for Daily Price Tracker Cloud Brain

import os
from datetime import datetime, timezone, timedelta

# ==========================================
# FILE & SHEET CONFIGURATION
# ==========================================
# Preferred primary workbook name
DEFAULT_WORKBOOK = "PriceTracker (6).xlsx"
FALLBACK_WORKBOOK = "PriceTracker.xlsx"

PRIMARY_SHEET = "Competitor price monitoring"
SECONDARY_SHEET = "Secondary_Competitor"
ARCHIVE_SHEET = "Competitor price monitoring__ARCHIVE__2026-08-25"
DEFAULT_SHEETS = [PRIMARY_SHEET]

SHEET_NAME = PRIMARY_SHEET

# Data row boundaries
FIRST_DATA_ROW = 3
LAST_DATA_ROW = 247  # Fallback row boundary (dynamic discovery used by default)

def find_matching_sheet_name(target_name: str, available_names: list) -> str:
    """Finds exact, case-insensitive, or prefix-matching sheet name from available sheets"""
    # 1. Exact match
    if target_name in available_names:
        return target_name

    target_clean = target_name.strip().lower().replace("_", "").replace(" ", "")
    # 2. Normalized match (ignoring case, spaces, and underscores)
    for name in available_names:
        clean = name.strip().lower().replace("_", "").replace(" ", "")
        if clean == target_clean:
            return name

    # 3. If target has 'archive' or 'oldsheet', prefer any sheet containing 'archive' or '__ar'
    if "archive" in target_clean or "oldsheet" in target_clean or "2026-08-25" in target_clean:
        for name in available_names:
            nl = name.strip().lower()
            if "archive" in nl or "__ar" in nl or "oldsheet" in nl:
                return name

    # 4. Truncated / longest prefix match (handles Excel 31-character tab limit)
    for name in sorted(available_names, key=len, reverse=True):
        clean = name.strip().lower().replace("_", "").replace(" ", "")
        if target_clean.startswith(clean) or clean.startswith(target_clean):
            return name

    return target_name

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
AMAZON_PINCODES = [
    # Exactly 20 major delivery pincodes across India covering all zones
    "110001",  # 1.  Delhi (Central) - Primary default
    "110031",  # 2.  Delhi (East)
    "400001",  # 3.  Mumbai (South)
    "400008",  # 4.  Mumbai (Central)
    "560001",  # 5.  Bangalore (Central)
    "560038",  # 6.  Bangalore (East)
    "500001",  # 7.  Hyderabad (Central)
    "500032",  # 8.  Hyderabad (West)
    "411001",  # 9.  Pune (Central)
    "411003",  # 10. Pune (North)
    "600001",  # 11. Chennai (North)
    "600003",  # 12. Chennai (Central)
    "700001",  # 13. Kolkata (Central)
    "700032",  # 14. Kolkata (South)
    "380001",  # 15. Ahmedabad
    "226001",  # 16. Lucknow
    "800001",  # 17. Patna
    "462001",  # 18. Bhopal
    "781001",  # 19. Guwahati
    "641001",  # 20. Coimbatore
]
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
        ist_tz = timezone(timedelta(hours=5, minutes=30))
        target_dt = datetime.now(ist_tz)
    
    day = target_dt.day
    if 11 <= (day % 100) <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    
    month_str = target_dt.strftime("%b")
    return f"{day}{suffix} - {month_str}"

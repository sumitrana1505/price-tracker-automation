# Daily Price Tracker Cloud Brain & Automation

An automated cloud-native solution to scrape competitor TV prices, bank offers, coupons, ratings, and rating counts from **Flipkart** and **Amazon India**, and record them directly into the **Price Tracker** Google Drive / Excel sheet daily without needing your laptop to be powered on.

---

## 1. Architecture Overview

```
       ┌────────────────────────────────────────────────────────┐
       │             GitHub Actions (Cloud Runner)              │
       │       Scheduled Daily at 08:30 AM IST (03:00 UTC)      │
       └───────────────────────────┬────────────────────────────┘
                                   │
               ┌───────────────────┴───────────────────┐
               ▼                                       ▼
    1. Download Latest Sheet                2. Fetch Competitors
       from Google Drive                       (Amazon & Flipkart)
       (via gdrive_sync.py)                    (via scrapers.py)
               │                                       │
               └───────────────────┬───────────────────┘
                                   │
                                   ▼
                   3. Update Date Block (6 Columns)
                      - Price (Live Selling Price)
                      - Cash Back / Bank_offer (Max Bank Offer)
                      - Coupon (Amazon coupons / 0)
                      - NEP (=IFERROR(Price-Offer-Coupon, 0))
                      - Rating (Live aggregate rating)
                      - Rating Count (Number of reviews/ratings)
                                   │
               ┌───────────────────┴───────────────────┐
               ▼                                       ▼
    4. Upload Updated Sheet                 5. Commit Git Backup
       back to Google Drive                    to GitHub Repository
```

---

## 2. Project Files

| File | Purpose |
| --- | --- |
| `price_tracker_engine.py` | **The Brain**: Coordinates scraping, workbook loading, column block creation, NEP formula generation, and auto-saving. |
| `scrapers.py` | **Scraper Engine**: High-resilience scrapers for Flipkart (`__INITIAL_STATE__`, `ATLAS_NEP_V2`, `fdpEventTracking`) and Amazon (mobile BuyBox, carousel offers, secondary view savings, ratings). |
| `config.py` | Central configuration: Sheet names, column coordinates, timing, date header formatting. |
| `gdrive_sync.py` | Cloud sync engine: Connects with Google Drive & Google Sheets using a Google Service Account. |
| `run_daily_tracker.bat` | One-click Windows runner to test or run manually locally. |
| `.github/workflows/daily_price_tracker.yml` | Cloud workflow that executes daily on GitHub's cloud servers (zero laptop required). |
| `requirements.txt` | Python dependencies. |

---

## 3. How to Run Locally

### Quick Test (Runs on first 3 products to verify live data)
```bash
python price_tracker_engine.py --test 3
```

### Full Run on All 247 Competitor Rows
```bash
python price_tracker_engine.py
```

### Full Run with Custom Date Header
```bash
python price_tracker_engine.py --date "16th - Oct"
```

### Or simply double-click:
`run_daily_tracker.bat`

---

## 4. How to Push & Automate on the Cloud (Zero-Laptop Setup)

### Step 1: Initialize Git and Push to GitHub
1. Open PowerShell in this folder: `c:\Users\OFFICE\Desktop\cloud`
2. Create a new repository on your GitHub account (e.g. named `price-tracker-cloud`).
3. Run:
```powershell
git init
git add .
git commit -m "Initial commit of Price Tracker Cloud Brain"
git branch -M main
git remote add origin https://github.com/<YOUR_GITHUB_USERNAME>/price-tracker-cloud.git
git push -u origin main
```

---

### Step 2: Connect Google Drive (Takes 2 minutes)
1. Go to [Google Cloud Console](https://console.cloud.google.com/).
2. Create a project and enable **Google Drive API** and **Google Sheets API**.
3. Create a **Service Account** under **IAM & Admin -> Service Accounts**.
4. Click on the Service Account -> **Keys** -> **Add Key** -> **Create new key (JSON)**. Download the JSON key file.
5. In your Google Drive, right-click your `PriceTracker` file (or folder) -> **Share** -> paste the Service Account email address with **Editor** access.
6. Copy the **File ID** from your Google Drive link:
   `https://drive.google.com/file/d/<FILE_ID>/view`

---

### Step 3: Add Secrets to GitHub Repository
1. On GitHub, go to your repository: **Settings** -> **Secrets and variables** -> **Actions** -> **New repository secret**.
2. Add secret `GDRIVE_FILE_ID`:
   - Value: The Google Drive File ID from Step 2.
3. Add secret `GDRIVE_SERVICE_ACCOUNT_JSON`:
   - Value: The entire contents of your downloaded Service Account `.json` file.

---

## 5. Automated Daily Schedule
Once the GitHub Secrets are added:
- GitHub Actions runs automatically **every day at 08:30 AM IST (03:00 UTC)**.
- It pulls the sheet from Google Drive, scrapes all prices, updates the 6-column block, writes back to Google Drive, and saves a backup in GitHub.
- **Your laptop does not need to be on or connected to the internet.**
- You can also manually trigger a run anytime by going to **Actions** -> **Daily Cloud Price Tracker** -> **Run workflow**.

# AZ_Ads_data – Amazon Ads daily numbers → Google Sheet

Workflow: `.github/workflows/az_ads_data.yml` (daily 10:15 AM IST, or *Run workflow* by hand).

1. opens Amazon Ads → Campaign manager → **Products** using saved login cookies
2. sets the date range to **yesterday** (or the date you give) and clicks **Export**
3. reads **Orders** (= Units Sold) and **Clicks** per `ASIN-ASIN` row
4. writes them into the `Ads_Data` tab, sections *Amazon Philips* and *Amazon Coocaa*,
   under that date (adds the 3 date columns in all four sections if they are missing)

## Secrets

| Secret | Value |
|---|---|
| `AMAZON_ADS_STORAGE_STATE` | whole content of `amazon_state.b64.txt` (made by `save_login.py`) |
| `GDRIVE_SERVICE_ACCOUNT_JSON` | already set – shared with the other workflows |
| `ADS_SPREADSHEET_ID` | only if `Ads_Data` is **not** in the `GSHEET_SPREADSHEET_ID` spreadsheet |

## When a run fails with `LOGIN NEEDED`

Amazon stopped accepting the saved cookies. On the office PC:

    cd az_ads
    python save_login.py

Sign in, press ENTER, then replace the `AMAZON_ADS_STORAGE_STATE` secret with the new
`amazon_state.b64.txt`. Missed days can be filled afterwards with *Run workflow* → custom date.

## Running on the PC

Needs three git-ignored files in `az_ads/`: `amazon_state.json`, `service_account.json`,
`sheet_id.txt` (the Google Sheet id or URL).

    python run.py                      yesterday
    python run.py --date 2026-11-13    a specific day
    python run.py --dry-run            show only, write nothing
    python run.py --file export.csv --date 2026-11-13    use a hand-made export

## Maintenance

- New model / ASIN: edit `config.json`.
- This repository is public: the workflow log shows no ads numbers and no export file is uploaded.
- "MTD Actual" formulas in the sheet are not extended for new date columns.

# FK_Ads_data – Flipkart Ads daily numbers → Google Sheet

Workflow: `.github/workflows/fk_ads_data.yml` (daily 10:45 AM IST, or *Run workflow* by hand).

1. opens Flipkart Ads with the saved login session
2. for each account in `config.json` (`Philips_2025_PO`, `Coocaa_RO_2025`): switches to it,
   Reports → **Other Reports** → PLA / Flipkart / **Consolidated FSN Report**, date **Yesterday**, Download
3. adds up **Units Sold (Direct)** (= Units Sold) and **Clicks** per FSN over both reports
   (same as the `FK_data` SUMIFS formulas in `Ads_data_file.xlsx`)
4. writes them into the `Ads_Data` tab, sections *Flipkart Coocaa* and *Flipkart Philips*,
   under that date (adds the 3 date columns if they are missing) and rebuilds the month's MTD Actual

The Google Sheet logic is shared with `az_ads/sheet_update.py`.

## Secrets

| Secret | Value |
|---|---|
| `FLIPKART_ADS_STORAGE_STATE` | whole content of `flipkart_state.b64.txt` (made by `save_login.py`) |
| `FLIPKART_ADS_EMAIL`, `FLIPKART_ADS_PASSWORD` | optional – used to sign in again when the saved session has expired |
| `GDRIVE_SERVICE_ACCOUNT_JSON`, `GSHEET_SPREADSHEET_ID` / `ADS_SPREADSHEET_ID` | already set for AZ_Ads_data |

## When a run fails with `LOGIN NEEDED`

    cd fk_ads
    python save_login.py

Sign in, press ENTER, then replace the `FLIPKART_ADS_STORAGE_STATE` secret with the new
`flipkart_state.b64.txt`.

## Running on the PC

Uses `fk_ads/flipkart_state.json` plus `az_ads/service_account.json` and `az_ads/sheet_id.txt`
(all git-ignored).

    python run.py                 yesterday
    python run.py --dry-run       show only, write nothing
    python run.py --skip-sheet    download + print only
    python run.py --date 2026-11-13 --file philips.csv --file coocaa.csv    reports downloaded by hand

Each report's own *Start Time / End Time* lines are checked against the date, so a report of
the wrong day is never written. Only **yesterday** is downloaded automatically; older days need `--file`.

## Maintenance

- New model / FSN, or a renamed ad account: edit `config.json`.
- This repository is public: the workflow log shows no ads numbers and no report file is uploaded.

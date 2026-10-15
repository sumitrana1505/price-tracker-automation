"""
Google Drive & Google Sheets Cloud Synchronization Module
==========================================================
Enables headless, automated download and upload of the Price Tracker
workbook to/from Google Drive, or direct sync with Google Sheets.

Credentials can be supplied via:
1. Local JSON file (service_account.json)
2. Environment variable: GDRIVE_SERVICE_ACCOUNT_JSON (Raw JSON string - ideal for GitHub Secrets)
"""

import io
import json
import logging
import os
import sys
from typing import Optional

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

logger = logging.getLogger("PriceTrackerGDrive")

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]


def get_google_credentials() -> Optional[service_account.Credentials]:
    """
    Resolves Google Service Account credentials from environment variable
    or local json file.
    """
    # 1. Environment variable with full JSON string (standard for GitHub Secrets)
    env_json = os.getenv("GDRIVE_SERVICE_ACCOUNT_JSON")
    if env_json:
        try:
            info = json.loads(env_json)
            creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
            logger.info("Loaded Google credentials from GDRIVE_SERVICE_ACCOUNT_JSON environment variable.")
            return creds
        except Exception as e:
            logger.error(f"Failed to parse credentials from GDRIVE_SERVICE_ACCOUNT_JSON: {e}")

    # 2. Local service_account.json file
    file_path = os.getenv("GDRIVE_CREDENTIALS_FILE", "service_account.json")
    if os.path.exists(file_path):
        try:
            creds = service_account.Credentials.from_service_account_file(file_path, scopes=SCOPES)
            logger.info(f"Loaded Google credentials from file: {file_path}")
            return creds
        except Exception as e:
            logger.error(f"Failed to load credentials from file {file_path}: {e}")

    logger.warning(
        "Google credentials not configured. To enable Google Drive sync, provide "
        "'service_account.json' or set the 'GDRIVE_SERVICE_ACCOUNT_JSON' environment variable."
    )
    return None


class GoogleDriveSync:
    """Manages downloading from and uploading to Google Drive"""

    def __init__(self, file_id: Optional[str] = None):
        self.file_id = file_id or os.getenv("GDRIVE_FILE_ID", "")
        self.creds = get_google_credentials()
        self.service = None
        if self.creds:
            self.service = build("drive", "v3", credentials=self.creds)

    def is_configured(self) -> bool:
        return self.service is not None and bool(self.file_id)

    def download_file(self, destination_path: str) -> bool:
        """Downloads the latest version of the workbook from Google Drive"""
        if not self.is_configured():
            logger.warning("Google Drive not configured. Skipping download.")
            return False

        logger.info(f"Downloading file ID '{self.file_id}' from Google Drive -> {destination_path}...")
        try:
            request = self.service.files().get_media(fileId=self.file_id)
            fh = io.FileIO(destination_path, "wb")
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                status, done = downloader.next_chunk()
                if status:
                    logger.info(f"  Download progress: {int(status.progress() * 100)}%")
            logger.info(f"File successfully downloaded from Google Drive to '{destination_path}'.")
            return True
        except Exception as e:
            logger.error(f"Failed to download from Google Drive: {e}")
            return False

    def upload_file(self, local_path: str) -> bool:
        """Uploads the updated workbook back to Google Drive (updating existing file)"""
        if not self.is_configured():
            logger.warning("Google Drive not configured. Skipping upload.")
            return False

        if not os.path.exists(local_path):
            logger.error(f"Cannot upload: local file '{local_path}' does not exist.")
            return False

        logger.info(f"Uploading '{local_path}' to Google Drive file ID '{self.file_id}'...")
        try:
            media = MediaFileUpload(
                local_path,
                mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                resumable=True
            )
            updated_file = self.service.files().update(
                fileId=self.file_id,
                media_body=media
            ).execute()
            logger.info(f"Successfully updated file on Google Drive (ID: {updated_file.get('id')}).")
            return True
        except Exception as e:
            logger.error(f"Failed to upload to Google Drive: {e}")
            return False


def test_connection():
    """Quick verification helper"""
    creds = get_google_credentials()
    if not creds:
        print("[FAIL] No valid credentials found.")
        return False
    try:
        service = build("drive", "v3", credentials=creds)
        results = service.files().list(pageSize=5, fields="files(id, name)").execute()
        files = results.get("files", [])
        print("[SUCCESS] Successfully connected to Google Drive API!")
        print(f"Accessible files count: {len(files)}")
        for f in files:
            print(f"  - {f['name']} (ID: {f['id']})")
        return True
    except Exception as e:
        print(f"[FAIL] Google Drive API connection failed: {e}")
        return False


if __name__ == "__main__":
    test_connection()

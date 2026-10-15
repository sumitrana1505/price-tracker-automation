@echo off
title Daily Price Tracker Brain
chcp 65001 >nul
setlocal enabledelayedexpansion

cd /d "%~dp0"

echo ======================================================
echo       Daily Price Tracker Automation Brain
echo ======================================================
echo.

set PYTHONIOENCODING=utf-8

where python >nul 2>nul
if %ERRORLEVEL% equ 0 (
    set "PYTHON_EXE=python"
    goto :RUN
)

where py >nul 2>nul
if %ERRORLEVEL% equ 0 (
    set "PYTHON_EXE=py"
    goto :RUN
)

echo [ERROR] Python was not found in PATH!
pause
exit /b 1

:RUN
echo Using Python: !PYTHON_EXE!
echo.
echo Select Run Mode:
echo [1] Run Quick Test (First 3 products)
echo [2] Run Full Daily Scrape on PriceTracker (All products)
echo [3] Run Full Daily Scrape with Google Drive Sync
echo.
set /p choice="Enter choice [1, 2, or 3]: "

if "%choice%"=="1" (
    echo Running test on 3 products...
    "!PYTHON_EXE!" price_tracker_engine.py --test 3
) else if "%choice%"=="2" (
    echo Running full scrape...
    "!PYTHON_EXE!" price_tracker_engine.py
) else if "%choice%"=="3" (
    echo Running full scrape with Google Drive sync...
    "!PYTHON_EXE!" price_tracker_engine.py --sync-gdrive
) else (
    echo Invalid choice. Running quick test by default...
    "!PYTHON_EXE!" price_tracker_engine.py --test 3
)

echo.
echo ======================================================
echo Execution completed.
echo ======================================================
pause

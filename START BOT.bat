@echo off
title NQ CALLS Bot
cd /d "%~dp0"
echo.
echo  =============================================
echo   NQ CALLS Bot - Starting Up
echo  =============================================
echo.

echo [1] Checking Python...
python --version
if errorlevel 1 (
    echo ERROR: Python not found!
    echo Please install from https://www.python.org/downloads
    echo Check "Add Python to PATH" during install!
    pause
    exit
)

echo.
echo [2] Installing required packages...
python -m pip install --upgrade pip --quiet
python -m pip install -r requirements.txt --quiet

echo.
echo [3] Starting NQ CALLS Bot...
echo  (Keep this window open while the bot runs)
echo.
python bot.py
echo.
echo  =============================================
echo   Bot stopped. See error above if crashed.
echo  =============================================
pause

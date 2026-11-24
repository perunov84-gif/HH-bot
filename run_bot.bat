@echo off
cd /d "%~dp0"
title HH Auto Agent - Telegram Service
echo ========================================================
echo Starting HH Auto Agent Telegram Service...
echo Waiting for commands from Telegram...
echo ========================================================

"C:\Python312\python.exe" -u telegram_service.py

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [ERROR] Process finished with exit code %ERRORLEVEL%
)
pause

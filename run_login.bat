@echo off
cd /d "%~dp0"
title HH Auto Agent - Login / Captcha
"C:\Python312\python.exe" -u login.py
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [ERROR] Process finished with exit code %ERRORLEVEL%
    pause
)

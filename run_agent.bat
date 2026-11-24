@echo off
cd /d "%~dp0"
title HH Auto Agent - Manual Run
echo ========================================================
echo Starting HH Auto Agent (Manual run)...
echo ========================================================

"C:\Python312\python.exe" -u agent.py

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [ERROR] Process finished with exit code %ERRORLEVEL%
)
echo.
echo Finished.
pause

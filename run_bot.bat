@echo off
chcp 65001 > nul
cd /d "%~dp0"
title HH Auto Agent - Telegram Service
echo ========================================================
echo Запуск Telegram-сервиса HH Auto Agent
echo Ожидание команд из Telegram...
echo ========================================================

if exist "C:\Python312\python.exe" (
    "C:\Python312\python.exe" -u telegram_service.py
) else (
    python -u telegram_service.py
)

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [ОШИБКА] Сервис завершился с кодом %ERRORLEVEL%
)
pause

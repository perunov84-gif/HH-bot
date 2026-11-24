@echo off
chcp 65001 > nul
cd /d "%~dp0"
title HH Auto Agent - Manual Run
echo ========================================================
echo Запуск HH.ru Авто-Агента (разовый запуск)
echo Время запуска: %date% %time%
echo ========================================================

if exist "C:\Python312\python.exe" (
    "C:\Python312\python.exe" -u agent.py
) else (
    python -u agent.py
)

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [ОШИБКА] Агент завершился с кодом %ERRORLEVEL%
)
echo.
echo Завершено.
pause

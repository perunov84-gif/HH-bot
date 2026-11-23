@echo off
chcp 65001 > nul
echo ========================================================
echo Остановка сервиса HH Auto Agent...
echo ========================================================

powershell -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*telegram_service*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force; Write-Host 'Остановлен процесс ID:' $_.ProcessId }"

echo.
echo Сервис HH Auto Agent полностью остановлен.
timeout /t 3 > nul

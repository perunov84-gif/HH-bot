@echo off
cd /d "%~dp0"
echo ========================================================
echo Stopping HH Auto Agent...
echo ========================================================

powershell -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*telegram_service*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force; Write-Host 'Stopped PID:' $_.ProcessId }"

echo.
echo Stopped successfully.
timeout /t 3 > nul

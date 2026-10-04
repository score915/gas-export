@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
rem Keep this many calendar days, including today. Always keep the latest date.
set "KEEP_DAYS=30"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\delete_old_data.ps1" -KeepDays %KEEP_DAYS% %*
set "TASK_RESULT=%errorlevel%"
echo.
if not "%GAS_EXPORT_NO_PAUSE%"=="1" pause
exit /b %TASK_RESULT%

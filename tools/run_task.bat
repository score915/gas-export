@echo off
setlocal
cd /d "%~dp0.."
chcp 65001 >nul
set "PYTHONUTF8=1"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0local_tasks.ps1" -Task "%~1"
set "TASK_RESULT=%errorlevel%"
echo.
if not "%GAS_EXPORT_NO_PAUSE%"=="1" pause
exit /b %TASK_RESULT%

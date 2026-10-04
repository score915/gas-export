@echo off
call "%~dp0tools\run_task.bat" restore
exit /b %errorlevel%

@echo off
call "%~dp0tools\run_task.bat" check
exit /b %errorlevel%

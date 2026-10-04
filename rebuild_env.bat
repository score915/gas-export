@echo off
call "%~dp0tools\run_task.bat" rebuild
exit /b %errorlevel%

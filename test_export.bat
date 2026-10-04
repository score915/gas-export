@echo off
call "%~dp0tools\run_task.bat" test
exit /b %errorlevel%

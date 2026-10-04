@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set "PYTHONUTF8=1"
if not exist ".venv\Scripts\python.exe" (
  echo 仮想環境がありません。README.ja.mdのインストール手順を実行してください。
  exit /b 2
)
".venv\Scripts\python.exe" run.py %*
exit /b %errorlevel%

@echo off
setlocal
cd /d "%~dp0"
title WTStat Discord Bot
if not exist ".venv\Scripts\python.exe" (
  python -m venv .venv
  if errorlevel 1 goto :fail
)
".venv\Scripts\python.exe" -c "import discord, PIL, fastapi" >nul 2>&1
if errorlevel 1 (
  python -m pip --python .venv install -r requirements-bot.txt
  if errorlevel 1 goto :fail
)
if not exist "bin\wtstat_results.exe" (
  echo Missing bin\wtstat_results.exe. See README.md.
  goto :fail
)
".venv\Scripts\python.exe" discord_bot.py
exit /b %errorlevel%
:fail
echo WTStat Discord Bot setup failed. Check the message above.
pause
exit /b 1

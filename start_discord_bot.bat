@echo off
setlocal
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
cd /d "%~dp0"
title WTStat Discord Bot
if not exist ".venv\Scripts\python.exe" (
  python -m venv .venv
  if errorlevel 1 goto :fail
)
".venv\Scripts\python.exe" -c "import discord, PIL, fastapi, playwright" >nul 2>&1
if errorlevel 1 (
  python -m pip --python .venv install -r requirements-bot.txt
  if errorlevel 1 goto :fail
)
if not exist "bin\wtstat_results.exe" (
  echo Missing bin\wtstat_results.exe. See README.md.
  goto :fail
)
".venv\Scripts\python.exe" discord_bot.py
if errorlevel 1 goto :fail
exit /b 0
:fail
echo WTStat Discord Bot stopped with an error. Check the message above.
pause
exit /b 1

@echo off
cd /d "%~dp0"
chcp 65001 >nul
if not exist ".venv\Scripts\python.exe" (
  echo Launch Start.cmd first to prepare Python.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -X utf8 configure_blizzard.py
pause

@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] .venv\Scripts\python.exe not found
  pause
  exit /b 1
)
echo Starting NTE Autoguitar with normal user rights...
".venv\Scripts\python.exe" main.py
if errorlevel 1 pause

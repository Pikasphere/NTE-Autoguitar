@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] .venv\Scripts\python.exe not found
  pause
  exit /b 1
)
echo Starting NTE Autoguitar as administrator...
powershell -NoProfile -Command "Start-Process -FilePath '%~dp0.venv\Scripts\python.exe' -ArgumentList '%~dp0main.py' -WorkingDirectory '%~dp0' -Verb RunAs"

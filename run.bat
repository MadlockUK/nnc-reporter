@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Not set up yet - run setup.bat first.
  pause
  exit /b 1
)
if not exist "config.json" copy config.example.json config.json >nul
".venv\Scripts\python.exe" app.py
if errorlevel 1 (
  echo.
  echo The app stopped with an error - the messages above say why.
  pause
)

@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Not set up yet - run setup.bat first.
  pause
  exit /b 1
)
if not exist ".env" copy .env.example .env >nul
".venv\Scripts\python.exe" app.py --open
if errorlevel 1 (
  echo.
  echo The app stopped with an error - the messages above say why.
  pause
)

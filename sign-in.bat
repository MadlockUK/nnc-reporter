@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run setup.bat first.
  pause
  exit /b 1
)
echo Signing in to the North Northamptonshire highways site...
echo The session is saved, so later reports will already be signed in.
".venv\Scripts\python.exe" app.py --login

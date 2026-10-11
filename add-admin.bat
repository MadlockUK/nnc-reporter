@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run setup.bat first.
  pause
  exit /b 1
)
set /p ADMINEMAIL=Email address for the first admin account: 
".venv\Scripts\python.exe" -m nncr.web.manage create-admin --email "%ADMINEMAIL%"
echo Start run.bat, then open the link above to set the password.
pause

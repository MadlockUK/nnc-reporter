@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
echo === NNC Reporter setup ===
echo.

rem Find a Python that actually runs. The Microsoft Store stub sits on PATH as
rem python.exe and only prints "Python was not found", so we test by executing it.
set "PY="
call :trypy py -3
call :trypy python
call :trypy python3
call :trypy "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
call :trypy "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
call :trypy "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
call :trypy "C:\Program Files\Python313\python.exe"
call :trypy "C:\Program Files\Python312\python.exe"
call :trypy "C:\Program Files\Python311\python.exe"

if not defined PY goto nopython

for /f "delims=" %%v in ('%PY% -c "import sys;print(sys.version.split()[0])"') do set "PYVER=%%v"
echo Using Python !PYVER!  ^(!PY!^)
echo.

echo Creating virtual environment...
%PY% -m venv .venv
if not exist ".venv\Scripts\python.exe" goto venvfail
set "VPY=.venv\Scripts\python.exe"

echo Installing packages ^(a few minutes^)...
"%VPY%" -m pip install --upgrade pip --quiet
if errorlevel 1 goto pipfail
"%VPY%" -m pip install -r requirements.txt
if errorlevel 1 goto pipfail

echo.
echo Installing the automation browser ^(Chromium, ~150MB^)...
"%VPY%" -m playwright install chromium
if errorlevel 1 goto pipfail

if not exist config.json (
  copy config.example.json config.json >nul
  echo.
  echo Created config.json - open it and add your details and API keys.
)

echo.
echo Running self-check...
"%VPY%" checkcode.py
"%VPY%" selfcheck.py
echo.
echo ============================================
echo  Setup complete. Double-click run.bat next.
echo ============================================
pause
exit /b 0


:trypy
rem %* = candidate command. Sets PY only if it runs and is Python 3.10 or newer.
if defined PY exit /b 0
set "CAND=%*"
%CAND% -c "import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>nul
if errorlevel 1 exit /b 0
set "PY=%CAND%"
exit /b 0


:nopython
echo ---------------------------------------------------------------
echo  No usable Python found.
echo.
echo  Windows ships a fake "python" that only opens the Microsoft
echo  Store - that is the error you saw. You need the real thing.
echo.
echo  EASIEST: copy this line, paste it into this window, press Enter:
echo.
echo      winget install -e --id Python.Python.3.12
echo.
echo  OR download it: https://www.python.org/downloads/windows/
echo      During install, TICK "Add python.exe to PATH".
echo.
echo  Then CLOSE this window, open setup.bat again.
echo ---------------------------------------------------------------
pause
exit /b 1

:venvfail
echo.
echo Could not create the virtual environment.
echo If this folder is synced by OneDrive and shows a cloud icon, right-click
echo the nnc-reporter folder and choose "Always keep on this device", then retry.
pause
exit /b 1

:pipfail
echo.
echo Package installation failed - see the messages above.
echo If it mentions a proxy or SSL, you may be behind a network filter.
pause
exit /b 1

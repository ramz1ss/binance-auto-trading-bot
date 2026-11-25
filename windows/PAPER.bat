@echo off
setlocal
cd /d "%~dp0.."
title Crypto Desk - PAPER TEST (no real money)
if not exist logs mkdir logs
if not exist state mkdir state
set "PY="
py -3 -c "import sys" >nul 2>nul && set "PY=py -3"
if not defined PY ( python -c "import sys" >nul 2>nul && set "PY=python" )
if not defined PY goto :nopython
%PY% -c "import sys; sys.exit(0 if sys.version_info>=(3,11) else 1)" >nul 2>nul
if errorlevel 1 goto :nopython
if not exist "venv\Scripts\python.exe" (
  echo Setting up Python for the desk - first run only, about a minute...
  %PY% -m venv venv
  if errorlevel 1 goto :fail
)
if not exist "venv\.deps_ok" (
  "venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
  if errorlevel 1 goto :fail
  echo ok> "venv\.deps_ok"
)
start "Crypto Desk Visualizer" /min cmd /c ""%~dp0VISUALIZER.bat" --mode paper"
echo PAPER test: pretend money (start_balance in config.toml), real Binance prices. Nothing is bought. Files go in the "paper" folder.
echo Close this window (or press Ctrl+C) to stop.
"venv\Scripts\python.exe" main.py --mode paper
echo.
pause
exit /b 0
:nopython
echo Python 3.11 or newer was not found. Install it from https://www.python.org/downloads/ (tick "Add Python to PATH"), then try again.
pause
exit /b 1
:fail
echo Setup failed. Check the internet connection and try again.
pause
exit /b 1

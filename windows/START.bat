@echo off
setlocal
cd /d "%~dp0.."
title Crypto Desk - LIVE (leave this window open)
if not exist logs mkdir logs
if not exist state mkdir state

rem ---- 1. find a working Python (3.11 or newer)
set "PY="
py -3 -c "import sys" >nul 2>nul && set "PY=py -3"
if not defined PY ( python -c "import sys" >nul 2>nul && set "PY=python" )
if not defined PY goto :nopython
%PY% -c "import sys; sys.exit(0 if sys.version_info>=(3,11) else 1)" >nul 2>nul
if errorlevel 1 goto :oldpython

rem ---- 2. private Python environment + requirements (first run only)
if not exist "venv\Scripts\python.exe" (
  echo Setting up Python for the desk - first run only, about a minute...
  %PY% -m venv venv
  if errorlevel 1 goto :venvfail
)
if not exist "venv\.deps_ok" (
  echo Installing requirements...
  "venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
  if errorlevel 1 goto :pipfail
  echo ok> "venv\.deps_ok"
)

rem ---- 3. open the live screen as its own app window (Edge/Chrome app mode, no address bar), then run the desk
start "Crypto Desk Visualizer" /min cmd /c ""%~dp0VISUALIZER.bat" --mode live"

rem ---- 4. run, and restart automatically after a crash
if exist "state\STOP" del /q "state\STOP"
if exist "state\TARGET_REACHED" goto :target
:loop
"venv\Scripts\python.exe" main.py --mode live
set "RC=%ERRORLEVEL%"
if exist "state\TARGET_REACHED" goto :target
if exist "state\STOP" goto :stopped
if "%RC%"=="0" goto :stopped
if "%RC%"=="2" goto :fixme
if "%RC%"=="3" (
  echo [%date% %time%] temporary problem ^(code 3^) - trying again in 60 seconds>> logs\supervisor.log
  echo.
  echo Temporary problem ^(internet / Binance^). Trying again in 60 seconds. Close this window to give up.
  timeout /t 60 /nobreak >nul
  goto :loop
)
echo [%date% %time%] desk exited with code %RC% - restarting in 10 seconds>> logs\supervisor.log
echo.
echo The desk stopped unexpectedly ^(code %RC%^). Restarting in 10 seconds... Close this window to give up.
timeout /t 10 /nobreak >nul
goto :loop

:target
echo.
echo ===== TARGET REACHED. The desk has stopped trading. See status.json. =====
pause
exit /b 0

:stopped
echo.
echo The desk has stopped. Any open position still has its stop-loss order on Binance.
pause
exit /b 0

:fixme
echo.
echo ===== The desk did NOT start. Read the [STOP] line above, fix it, then double-click START.bat again. =====
pause
exit /b 2

:nopython
echo.
echo Python was not found. Install it from https://www.python.org/downloads/ and TICK "Add Python to PATH" in the installer, then run START.bat again.
pause
exit /b 1
:oldpython
echo.
echo Your Python is too old. Install the newest Python from https://www.python.org/downloads/ (tick "Add Python to PATH"), then run START.bat again.
pause
exit /b 1
:venvfail
echo.
echo Could not set up Python for the desk. Take a photo of this window and ask for help.
pause
exit /b 1
:pipfail
echo.
echo Could not download the requirements. Check that the internet works and run START.bat again.
pause
exit /b 1

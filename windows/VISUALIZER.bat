@echo off
setlocal
cd /d "%~dp0.."
title Crypto Desk - visualizer server (leave open; the app window is separate)
set "PY="
if exist "venv\Scripts\python.exe" set "PY=venv\Scripts\python.exe"
if not defined PY ( py -3 -c "import sys" >nul 2>nul && set "PY=py -3" )
if not defined PY ( python -c "import sys" >nul 2>nul && set "PY=python" )
if not defined PY goto :nopython
rem opens Microsoft Edge in app mode (msedge --app=... --window-size=1400,900), else Chrome app mode, else the default browser
%PY% visualizer.py --open %*
if errorlevel 1 (
  echo.
  echo The visualizer could not start - read the message above.
  pause
)
exit /b 0
:nopython
echo.
echo Python was not found. Install it from https://www.python.org/downloads/ and TICK "Add Python to PATH", then run VISUALIZER.bat again.
pause
exit /b 1

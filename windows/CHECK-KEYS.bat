@echo off
setlocal
cd /d "%~dp0.."
if not exist logs mkdir logs
if not exist state mkdir state
if not exist "venv\Scripts\python.exe" (
  echo Please run START.bat or PAPER.bat once first so Python gets set up, then try again.
  pause
  exit /b 1
)
"venv\Scripts\python.exe" main.py --mode live --preflight-only
echo.
pause

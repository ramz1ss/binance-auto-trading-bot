@echo off
setlocal
cd /d "%~dp0.."
if exist "venv\Scripts\python.exe" ( "venv\Scripts\python.exe" status.py ) else ( py -3 status.py || python status.py )
echo.
pause

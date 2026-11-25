@echo off
setlocal
cd /d "%~dp0.."
if exist "venv\Scripts\python.exe" ( "venv\Scripts\python.exe" status.py paper ) else ( py -3 status.py paper || python status.py paper )
echo.
pause

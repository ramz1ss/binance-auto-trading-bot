@echo off
setlocal
cd /d "%~dp0.."
set MODE=live
if /i "%~1"=="paper" set MODE=paper
set PY=
if exist "venv\Scripts\python.exe" set "PY=venv\Scripts\python.exe"
if not defined PY ( where py >nul 2>nul && set "PY=py -3" )
if not defined PY set "PY=python"
%PY% deskctl.py resume --mode %MODE%
echo.
pause

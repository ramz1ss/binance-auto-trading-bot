@echo off
setlocal
cd /d "%~dp0.."
if not exist state mkdir state
echo stop> "state\STOP"
echo Asking the desk to stop... (up to 40 seconds)
set /a n=0
:wait
if not exist "state\desk.pid" goto :done
set /a n+=1
if %n% GEQ 40 goto :force
timeout /t 1 /nobreak >nul
goto :wait
:force
set /p PID=<"state\desk.pid"
taskkill /PID %PID% /F >nul 2>nul
del /q "state\desk.pid" >nul 2>nul
:done
echo.
echo The desk is stopped. If a trade was open, its stop-loss order is still active on Binance.
echo To start again, double-click START.bat.
pause

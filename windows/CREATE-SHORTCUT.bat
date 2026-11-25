@echo off
setlocal
cd /d "%~dp0.."
echo Creating the "Crypto Desk" shortcut (Desktop + Start menu)...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0create_shortcut.ps1"
if errorlevel 1 (
  echo.
  echo Could not create the shortcut. You can still start the screen with VISUALIZER.bat.
) else (
  echo.
  echo Done. Look for "Crypto Desk" on your Desktop - double-click it to open the live screen.
  echo Keep this folder where it is: the shortcut points to it.
)
echo.
pause

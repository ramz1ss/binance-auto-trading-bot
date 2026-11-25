@echo off
rem Same as VISUALIZER.bat but ALSO reachable from a phone on the same Wi-Fi (read-only on the phone; the CLOSE TRADE NOW button works on the PC, or on the phone only with --allow-control + token).
cd /d "%~dp0.."
call "%~dp0VISUALIZER.bat" --host 0.0.0.0 %*

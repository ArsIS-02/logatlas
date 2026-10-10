@echo off
rem LogAtlas: collect logs from a Linux host over SSH and send them to LogAtlas. Double-click to run.
rem collect-linux-remote.ps1 and collect-linux.sh must be in the same folder as this file.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0collect-linux-remote.ps1" %*
echo.
pause

@echo off
rem LogAtlas: install and start the LogAtlas Windows service (web UI for the local network).
rem Asks for administrator rights, then runs service\service.ps1 -Action install.
fltmc >nul 2>&1 || (
  powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0service\service.ps1" -Action install
echo.
pause

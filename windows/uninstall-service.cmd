@echo off
rem LogAtlas: stop and remove the LogAtlas Windows service. Data in %ProgramData%\LogAtlas is kept.
rem Asks for administrator rights, then runs service\service.ps1 -Action uninstall.
fltmc >nul 2>&1 || (
  powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0service\service.ps1" -Action uninstall
echo.
pause

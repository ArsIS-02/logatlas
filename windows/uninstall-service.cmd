@echo off
rem LogAtlas: stop and remove the LogAtlas Windows service. Collected logs in %ProgramData%\LogAtlas are kept.
rem The service is removed by LogAtlas.exe itself; it asks Windows for administrator rights.
"%~dp0LogAtlas.exe" --uninstall-service
if errorlevel 1 echo LogAtlas: the service was not removed.
echo.
pause

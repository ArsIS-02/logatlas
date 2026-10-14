@echo off
rem LogAtlas: install and start the LogAtlas Windows service — the panel then works without signing in
rem and is available from other computers in the local network.
rem The service is installed by LogAtlas.exe itself; it asks Windows for administrator rights.
"%~dp0LogAtlas.exe" --install-service
if errorlevel 1 echo LogAtlas: the service was not installed.
echo.
pause

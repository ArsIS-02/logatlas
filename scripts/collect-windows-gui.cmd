@echo off
rem LogAtlas: collect Windows event logs with a progress window. Just double-click this file.
rem Windows asks for administrator rights: they are needed to read the Security log.
rem The script only reads logs: nothing is changed and nothing is sent anywhere.
rem
rem This file is only a small launcher. Keep collect-windows-gui.ps1 (the collector itself) in the
rem same folder: the launcher starts it in PowerShell and passes its arguments along.
rem
rem LogAtlas address for the "Open" button (optional), e.g. http://192.168.10.10:8484
set "LOGATLAS_URL="
rem
rem Troubleshooting: run "collect-windows-gui.cmd debug" from a console to keep the PowerShell window visible.
setlocal
set "LA_WIN="
set "LA_NOEXIT="
if /i "%~1"=="debug" (
  set "LA_WIN=-WindowStyle Normal"
  set "LA_NOEXIT=-NoExit"
)
if not exist "%~dp0collect-windows-gui.ps1" (
  echo LogAtlas: collect-windows-gui.ps1 was not found next to this file.
  echo Put collect-windows-gui.cmd and collect-windows-gui.ps1 in the same folder and run the .cmd again.
  pause
  exit /b 1
)
rem Administrator rights are requested by collect-windows-gui.ps1 itself (UAC), so no elevation here.
start "" powershell -NoProfile -STA -ExecutionPolicy Bypass %LA_WIN% %LA_NOEXIT% -File "%~dp0collect-windows-gui.ps1" %*
exit /b

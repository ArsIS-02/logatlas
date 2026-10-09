# Сбор журналов Windows для LogAtlas. Запуск от администратора:
#   powershell -ExecutionPolicy Bypass -File collect-windows.ps1
$out = Join-Path $env:TEMP ("logatlas_" + (Get-Date -Format yyyyMMdd_HHmm))
New-Item -ItemType Directory -Path $out | Out-Null
$logs = @("Security", "System", "Application",
          "Microsoft-Windows-Sysmon/Operational",
          "Microsoft-Windows-PowerShell/Operational",
          "Microsoft-Windows-Windows Defender/Operational",
          "Microsoft-Windows-TaskScheduler/Operational",
          "Microsoft-Windows-TerminalServices-LocalSessionManager/Operational",
          "Microsoft-Windows-TerminalServices-RemoteConnectionManager/Operational")
foreach ($l in $logs) {
  $name = ($l -replace '[/ ]', '_') + ".evtx"
  wevtutil epl $l (Join-Path $out $name) 2>$null
}
$zip = Join-Path ([Environment]::GetFolderPath("Desktop")) ("$env:COMPUTERNAME-logs.zip")
Compress-Archive -Path "$out\*" -DestinationPath $zip -Force
Remove-Item -Recurse -Force $out
Write-Host "Готово: $zip"

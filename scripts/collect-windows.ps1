<#
Сбор журналов Windows для LogAtlas.

Запуск — в PowerShell, открытом от имени администратора:
  cd C:\путь\к\logatlas\scripts
  powershell -ExecutionPolicy Bypass -File .\collect-windows.ps1

Параметры:
  -OutDir <папка>  куда положить архив (по умолчанию — рабочий стол)
  -KeepTemp        не удалять временную папку с .evtx после упаковки
  -NoOpen          не открывать Проводник с готовым архивом

Файл сохранён в UTF-8 с BOM: без BOM Windows PowerShell 5.1 читает его как ANSI и ломает кириллицу.
#>
param(
  [string]$OutDir = [Environment]::GetFolderPath('Desktop'),
  [switch]$KeepTemp,
  [switch]$NoOpen
)

function Say($text, $color = 'Gray') { Write-Host $text -ForegroundColor $color }

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
  [Security.Principal.WindowsBuiltInRole]::Administrator)

Say ''
Say 'LogAtlas — сбор журналов Windows' 'Cyan'
Say ('  Компьютер:     {0}' -f $env:COMPUTERNAME)
Say ('  Пользователь:  {0}\{1}' -f $env:USERDOMAIN, $env:USERNAME)
Say ('  Администратор: {0}' -f $(if ($admin) { 'да' } else { 'нет' }))

if (-not $admin) {
  Say ''
  Say 'Нужны права администратора (иначе журнал Security недоступен).' 'Yellow'
  Say 'Откройте PowerShell так: Пуск → введите PowerShell → правой кнопкой → «Запуск от имени администратора»,' 'Yellow'
  Say ('затем: cd "{0}"; powershell -ExecutionPolicy Bypass -File .\collect-windows.ps1' -f $PSScriptRoot) 'Yellow'
  exit 1
}

if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Path $OutDir -Force | Out-Null }
$OutDir = (Resolve-Path $OutDir).Path

$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$tmp = Join-Path $env:TEMP ('logatlas_' + $stamp)
New-Item -ItemType Directory -Path $tmp -Force | Out-Null
$zip = Join-Path $OutDir ('{0}-logs-{1}.zip' -f $env:COMPUTERNAME, $stamp)

Say ''
Say ('  Временная папка: {0}' -f $tmp)
Say ('  Архив будет:     {0}' -f $zip)
Say ''

$logs = @(
  'Security', 'System', 'Application',
  'Microsoft-Windows-Sysmon/Operational',
  'Microsoft-Windows-PowerShell/Operational',
  'Microsoft-Windows-Windows Defender/Operational',
  'Microsoft-Windows-TaskScheduler/Operational',
  'Microsoft-Windows-TerminalServices-LocalSessionManager/Operational',
  'Microsoft-Windows-TerminalServices-RemoteConnectionManager/Operational'
)

$saved = 0
$i = 0
foreach ($l in $logs) {
  $i++
  $prefix = '  [{0}/{1}] {2}' -f $i, $logs.Count, $l
  Write-Progress -Activity 'Выгрузка журналов' -Status $l -PercentComplete ([int](100 * ($i - 1) / $logs.Count))

  $info = & wevtutil.exe gli $l 2>$null
  if ($LASTEXITCODE -ne 0) {
    Say ('{0} — нет такого журнала' -f $prefix) 'DarkGray'
    continue
  }
  $count = ($info | Select-String 'numberOfLogRecords:\s*(\d+)').Matches | ForEach-Object { $_.Groups[1].Value }
  if (-not $count) { $count = '?' }

  $dest = Join-Path $tmp (($l -replace '[/ ]', '_') + '.evtx')
  & wevtutil.exe epl $l $dest 2>$null
  if ($LASTEXITCODE -eq 0 -and (Test-Path $dest)) {
    $mb = '{0:N1}' -f ((Get-Item $dest).Length / 1MB)
    Say ('{0} — {1} событий, {2} МБ' -f $prefix, $count, $mb) 'Green'
    $saved++
  } else {
    Say ('{0} — не удалось выгрузить (код {1})' -f $prefix, $LASTEXITCODE) 'Red'
  }
}
Write-Progress -Activity 'Выгрузка журналов' -Completed

if ($saved -eq 0) {
  Say ''
  Say 'Не удалось выгрузить ни одного журнала.' 'Red'
  Say ('Временная папка оставлена: {0}' -f $tmp)
  exit 1
}


Say ''
Say 'Упаковка…'
try {
  Compress-Archive -Path (Join-Path $tmp '*') -DestinationPath $zip -Force -ErrorAction Stop
} catch {
  Say ('Не удалось создать архив: {0}' -f $_.Exception.Message) 'Red'
  Say ('Журналы остались здесь: {0}' -f $tmp)
  exit 1
}

if ($KeepTemp) {
  Say ('Временная папка сохранена: {0}' -f $tmp)
} else {
  Remove-Item -Recurse -Force $tmp
}

$zipMb = '{0:N1}' -f ((Get-Item $zip).Length / 1MB)
Say ''
Say ('Готово: {0} ({1} МБ, журналов: {2})' -f $zip, $zipMb, $saved) 'Green'
Say 'Загрузите этот архив в LogAtlas.'

if (-not $NoOpen) { Start-Process explorer.exe -ArgumentList ('/select,"{0}"' -f $zip) }

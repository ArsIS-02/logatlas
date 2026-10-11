<#
Служба Windows для LogAtlas: панель работает без входа пользователя и доступна с других компьютеров сети.

  powershell -ExecutionPolicy Bypass -File service.ps1 -Action install     установить и запустить
  powershell -ExecutionPolicy Bypass -File service.ps1 -Action uninstall   остановить и удалить
  -Action start | stop | status

Нужны права администратора (проще запускать install-service.cmd / uninstall-service.cmd — они попросят сами).
Данные службы — %ProgramData%\LogAtlas, журналы службы — %ProgramData%\LogAtlas\logs.
Правило брандмауэра пускает к порту панели только из локальной подсети (профили «Домен» и «Частная сеть»).

Файл сохранён в UTF-8 с BOM: без BOM Windows PowerShell 5.1 читает его как ANSI и ломает кириллицу.
#>
param(
  [ValidateSet('install', 'uninstall', 'start', 'stop', 'status')]
  [string]$Action = 'install',
  [switch]$Quiet
)
$ErrorActionPreference = 'Stop'
$Name = 'LogAtlas'
$Rule = 'LogAtlas'
$winsw = Join-Path $PSScriptRoot 'LogAtlasService.exe'
$data = Join-Path $env:ProgramData 'LogAtlas'

function Say($text, $color = 'Gray') { if (-not $Quiet) { Write-Host $text -ForegroundColor $color } }

function Get-Port {
  $p = 8484
  try {
    $f = Join-Path $data 'settings.json'
    if (Test-Path -LiteralPath $f) {
      $j = [IO.File]::ReadAllText($f) | ConvertFrom-Json
      if ($j.port) { $p = [int]$j.port }
    }
  } catch { }
  return $p
}

function Invoke-WinSW([string]$cmd) {
  # Windows PowerShell 5.1 при Stop превращает вывод программы в stderr в исключение — здесь он нужен как текст
  $ErrorActionPreference = 'Continue'
  $out = & $winsw $cmd 2>&1 | Out-String
  return @{ Code = $LASTEXITCODE; Text = $out.Trim() }
}

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
  [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) {
  Say 'Нужны права администратора: запустите install-service.cmd или PowerShell от имени администратора.' 'Yellow'
  exit 1
}
if (-not (Test-Path -LiteralPath $winsw)) {
  Say ('Не найден {0}' -f $winsw) 'Red'
  exit 1
}

$svc = Get-Service -Name $Name -ErrorAction SilentlyContinue

switch ($Action) {
  'install' {
    if ($svc) {
      Say 'Служба уже установлена — переустанавливаю (пути могли измениться).'
      if ($svc.Status -ne 'Stopped') { Invoke-WinSW 'stop' | Out-Null; Start-Sleep -Seconds 2 }
      Invoke-WinSW 'uninstall' | Out-Null
      Start-Sleep -Seconds 2
    }
    New-Item -ItemType Directory -Force -Path (Join-Path $data 'logs') | Out-Null
    $r = Invoke-WinSW 'install'
    if ($r.Code -ne 0) { Say ('Не удалось установить службу: {0}' -f $r.Text) 'Red'; exit 1 }
    $port = Get-Port
    try {
      Get-NetFirewallRule -DisplayName $Rule -ErrorAction SilentlyContinue | Remove-NetFirewallRule
      New-NetFirewallRule -DisplayName $Rule -Description 'LogAtlas: веб-интерфейс для компьютеров локальной сети' `
        -Direction Inbound -Action Allow -Protocol TCP -LocalPort $port -RemoteAddress LocalSubnet `
        -Profile Domain, Private | Out-Null
      Say ('Брандмауэр: порт {0} открыт для локальной подсети (профили «Домен» и «Частная»).' -f $port)
    } catch {
      Say ('Правило брандмауэра не создано: {0}' -f $_.Exception.Message) 'Yellow'
    }
    $r = Invoke-WinSW 'start'
    if ($r.Code -ne 0) { Say ('Служба установлена, но не запустилась: {0}' -f $r.Text) 'Red'; exit 1 }
    $ok = $false
    for ($i = 0; $i -lt 60; $i++) {
      try {
        $resp = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 -Uri ('http://127.0.0.1:{0}/healthz' -f (Get-Port))
        if ($resp.StatusCode -eq 200) { $ok = $true; break }
      } catch { }
      Start-Sleep -Seconds 1
    }
    $port = Get-Port
    if ($ok) {
      Say ''
      Say 'Служба LogAtlas работает.' 'Green'
      Say ('  На этом компьютере:  http://127.0.0.1:{0}/' -f $port)
      $ips = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } | ForEach-Object { $_.IPAddress })
      foreach ($ip in $ips) { Say ('  Из сети:             http://{0}:{1}/' -f $ip, $port) }
      Say ''
      Say 'В LogAtlas нет входа по паролю: открывайте порт только в доверенной сети.' 'Yellow'
    } else {
      Say ('Служба запущена, но панель пока не отвечает. Журнал службы: {0}' -f (Join-Path $data 'logs')) 'Yellow'
    }
  }
  'uninstall' {
    if ($svc) {
      if ($svc.Status -ne 'Stopped') { Invoke-WinSW 'stop' | Out-Null; Start-Sleep -Seconds 2 }
      $r = Invoke-WinSW 'uninstall'
      if ($r.Code -ne 0) { Say ('Не удалось удалить службу: {0}' -f $r.Text) 'Red'; exit 1 }
      Say 'Служба LogAtlas удалена.' 'Green'
    } else {
      Say 'Служба LogAtlas не установлена.'
    }
    try { Get-NetFirewallRule -DisplayName $Rule -ErrorAction SilentlyContinue | Remove-NetFirewallRule } catch { }
    Say ('Данные службы остались в {0} — удалите папку, если они больше не нужны.' -f $data)
  }
  'start'  { $r = Invoke-WinSW 'start'; Say $r.Text; exit $r.Code }
  'stop'   { if ($svc -and $svc.Status -ne 'Stopped') { $r = Invoke-WinSW 'stop'; Say $r.Text; exit $r.Code } }
  'status' { if ($svc) { Say ('{0}: {1}' -f $Name, $svc.Status) } else { Say 'Служба LogAtlas не установлена.' } }
}
exit 0

<#
Проверка сборки LogAtlas для Windows (запускается в GitHub Actions после windows\build.ps1, нужны права
администратора): переносной zip, установщик со службой, режим приложения, удаление.

Файл сохранён в UTF-8 с BOM: без BOM Windows PowerShell 5.1 читает его как ANSI и ломает кириллицу.
#>
param([Parameter(Mandatory = $true)][string]$Version)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$repo = Split-Path $PSScriptRoot -Parent
$distRoot = Join-Path $repo 'dist'
$demo = Join-Path $repo 'demo\windows-demo.zip'
$work = Join-Path ($env:RUNNER_TEMP, $env:TEMP | Where-Object { $_ } | Select-Object -First 1) 'logatlas-smoke'
if (Test-Path $work) { Remove-Item -Recurse -Force $work }
New-Item -ItemType Directory -Force -Path $work | Out-Null

function Step($t) { Write-Host "==> $t" -ForegroundColor Cyan }
function Fail($t) { throw "ПРОВЕРКА НЕ ПРОЙДЕНА: $t" }

function Wait-Instance($dataDir, $sec = 90) {
  $f = Join-Path $dataDir 'instance.json'
  for ($i = 0; $i -lt $sec; $i++) {
    if (Test-Path $f) {
      $j = Get-Content -Raw $f | ConvertFrom-Json
      if (Test-Health $j.port) { return [int]$j.port }
    }
    Start-Sleep -Seconds 1
  }
  $log = Join-Path $dataDir 'logatlas.log'
  if (Test-Path $log) { Get-Content $log -Tail 40 | Write-Host }
  Fail "панель не запустилась (нет ответа, $f)"
}

function Test-Health($port) {
  try { return (Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 -Uri "http://127.0.0.1:$port/healthz").StatusCode -eq 200 } catch { return $false }
}

function Wait-Health($port, $sec = 90) {
  for ($i = 0; $i -lt $sec; $i++) { if (Test-Health $port) { return }; Start-Sleep -Seconds 1 }
  Fail "порт $port не отвечает"
}

function Get-Meta($port) { Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/meta" }

function Test-Demo($port, $label) {
  $resp = & curl.exe -sS -F "files=@$demo" -F "name=smoke $label" "http://127.0.0.1:$port/api/cases"
  if ($LASTEXITCODE -ne 0) { Fail "загрузка демо ($label)" }
  $id = ($resp | ConvertFrom-Json).id
  if (-not $id) { Fail "загрузка демо ($label): $resp" }
  for ($i = 0; $i -lt 240; $i++) {
    $c = Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/cases/$id"
    if ($c.status -eq 'ready') {
      if ([int]$c.events -le 0) { Fail "демо разобрано без событий ($label)" }
      Write-Host ("   демо ({0}): {1} событий" -f $label, $c.events) -ForegroundColor Green
      $ov = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$port/api/cases/$id/overview"
      if ($ov.StatusCode -ne 200) { Fail "сводка ($label)" }
      return
    }
    if ($c.status -eq 'error') { Fail "разбор демо ($label): $($c.message)" }
    Start-Sleep -Seconds 1
  }
  Fail "разбор демо не закончился за 4 минуты ($label)"
}

function Wait-NoProcess($under, $sec = 30) {
  for ($i = 0; $i -lt $sec; $i++) {
    $p = @(Get-Process python, pythonw -ErrorAction SilentlyContinue | Where-Object { $_.Path -and $_.Path.StartsWith($under, 'OrdinalIgnoreCase') })
    if (-not $p) { return }
    Start-Sleep -Seconds 1
  }
  Fail "процессы LogAtlas из $under не завершились: $($p.Id -join ', ')"
}

# ---------------------------------------------------------------- переносной zip
Step 'переносной zip'
$zip = Join-Path $distRoot "LogAtlas-$Version-portable-x64.zip"
Expand-Archive -Path $zip -DestinationPath $work
$proot = Join-Path $work "LogAtlas-$Version-portable-x64"
if (-not (Test-Path (Join-Path $proot 'portable.txt'))) { Fail 'в zip нет portable.txt' }
Start-Process -FilePath (Join-Path $proot 'LogAtlas.exe')
$port = Wait-Instance (Join-Path $proot 'data')
$m = Get-Meta $port
if ($m.runtime -ne 'windows-portable') { Fail "runtime = $($m.runtime)" }
if (-not $m.local_collect) { Fail 'нет «Этот компьютер» в приложении' }
if ($m.version -ne $Version) { Write-Host "   версия панели $($m.version), сборки $Version" -ForegroundColor Yellow }
Test-Demo $port 'portable'
Start-Process -FilePath (Join-Path $proot 'LogAtlas.exe') -ArgumentList '--quit' -Wait
Wait-NoProcess $proot
Write-Host '   закрыт по --quit, воркеры разбора завершились' -ForegroundColor Green

# ---------------------------------------------------------------- установщик + служба
Step 'установщик со службой'
$setup = Join-Path $distRoot "LogAtlas-Setup-$Version-x64.exe"
$inst = Join-Path $work 'Program Files\LogAtlas'
$p = Start-Process -FilePath $setup -Wait -PassThru -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART',
  '/ALLUSERS', '/TASKS=service', "/DIR=`"$inst`"", "/LOG=`"$work\setup.log`"")
if ($p.ExitCode -ne 0) { Get-Content "$work\setup.log" -Tail 40 | Write-Host; Fail "установщик: код $($p.ExitCode)" }
$svc = Get-Service LogAtlas -ErrorAction SilentlyContinue
if (-not $svc) { Get-Content "$work\setup.log" -Tail 40 | Write-Host; Fail 'служба не установлена' }
Wait-Health 8484 120
if ((Get-Service LogAtlas).Status -ne 'Running') { Fail 'служба не запущена' }
$m = Get-Meta 8484
if ($m.runtime -ne 'windows-service') { Fail "runtime службы = $($m.runtime)" }
if ($m.local_collect) { Fail '«Этот компьютер» не должен быть в службе' }
Test-Demo 8484 'служба'
$ci = Invoke-RestMethod -Uri 'http://127.0.0.1:8484/api/collect/info'
if ($ci.available -and $ci.pubkey -like 'ssh-ed25519 *') {
  Write-Host '   ключ панели для сбора по SSH создан' -ForegroundColor Green
  $key = Join-Path $env:ProgramData 'LogAtlas\keys\logatlas_ed25519'
  $acl = (Get-Acl $key).Access | ForEach-Object { $_.IdentityReference.Value }
  Write-Host ("   доступ к ключу: {0}" -f ($acl -join ', '))
  if ($acl | Where-Object { $_ -match 'Users|Пользователи|Everyone|Все' }) { Fail 'ключ панели доступен всем пользователям' }
} else {
  Write-Host "   сбор по SSH недоступен: $($ci.unavailable)" -ForegroundColor Yellow
}
if (-not (Get-NetFirewallRule -DisplayName 'LogAtlas' -ErrorAction SilentlyContinue)) { Fail 'нет правила брандмауэра' }

# ---------------------------------------------------------------- режим приложения рядом со службой
Step 'приложение (рядом со службой)'
Start-Process -FilePath (Join-Path $inst 'LogAtlas.exe') -ArgumentList '--app'
$port = Wait-Instance (Join-Path $env:LOCALAPPDATA 'LogAtlas')
if ($port -eq 8484) { Fail 'приложение заняло порт службы' }
$m = Get-Meta $port
if ($m.runtime -ne 'windows-app') { Fail "runtime приложения = $($m.runtime)" }
Write-Host "   приложение на порту $port" -ForegroundColor Green
Start-Process -FilePath (Join-Path $inst 'LogAtlas.exe') -ArgumentList '--quit' -Wait
Start-Sleep -Seconds 3
if (Test-Health $port) { Fail 'приложение не закрылось по --quit' }

# ---------------------------------------------------------------- удаление
Step 'удаление'
Start-Process -FilePath (Join-Path $inst 'unins000.exe') -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART') -Wait
for ($i = 0; $i -lt 90 -and (Test-Path (Join-Path $inst 'unins000.exe')); $i++) { Start-Sleep -Seconds 1 }
if (Get-Service LogAtlas -ErrorAction SilentlyContinue) { Fail 'служба осталась после удаления' }
if (Test-Path (Join-Path $inst 'python')) { Fail 'файлы остались после удаления' }
if (Get-NetFirewallRule -DisplayName 'LogAtlas' -ErrorAction SilentlyContinue) { Fail 'правило брандмауэра осталось' }
Wait-NoProcess $inst

Write-Host 'Все проверки пройдены' -ForegroundColor Green

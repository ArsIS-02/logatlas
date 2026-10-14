<#
Проверка сборки LogAtlas для Windows (запускается в GitHub Actions после windows\build.ps1, нужны права
администратора): переносной zip с окном, установщик со службой, окно рядом со службой, удаление.
Окна сообщений отключены переменной LOGATLAS_NO_DIALOGS=1 — их некому закрыть.

Файл сохранён в UTF-8 с BOM: без BOM Windows PowerShell 5.1 читает его как ANSI и ломает кириллицу.
#>
param(
  [Parameter(Mandatory = $true)][string]$Version,
  [Parameter(Mandatory = $true)][ValidateSet('x64', 'arm64')][string]$Arch
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$env:LOGATLAS_NO_DIALOGS = '1'
$repo = Split-Path $PSScriptRoot -Parent
$distRoot = Join-Path $repo 'dist'
$demo = Join-Path $repo 'demo\windows-demo.zip'
$work = Join-Path ($env:RUNNER_TEMP, $env:TEMP | Where-Object { $_ } | Select-Object -First 1) 'logatlas-smoke'
if (Test-Path $work) { Remove-Item -Recurse -Force $work }
New-Item -ItemType Directory -Force -Path $work | Out-Null

function Step($t) { Write-Host "==> $t" -ForegroundColor Cyan }
function Fail($t) { throw "ПРОВЕРКА НЕ ПРОЙДЕНА: $t" }

function Show-Logs($dataDir) {
  foreach ($n in 'window.log', 'logatlas.log') {
    $f = Join-Path $dataDir $n
    if (Test-Path $f) { Write-Host "---- $f"; Get-Content $f -Tail 40 | Write-Host }
  }
}

function Test-Health($port) {
  try { return (Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 -Uri "http://127.0.0.1:$port/healthz").StatusCode -eq 200 } catch { return $false }
}

function Wait-Instance($dataDir, $sec = 120) {
  $f = Join-Path $dataDir 'instance.json'
  for ($i = 0; $i -lt $sec; $i++) {
    if (Test-Path $f) {
      $j = Get-Content -Raw $f | ConvertFrom-Json
      if (Test-Health $j.port) { return [int]$j.port }
    }
    Start-Sleep -Seconds 1
  }
  Show-Logs $dataDir
  Fail "панель не запустилась (нет ответа, $f)"
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

function Get-Window($root) {
  @(Get-Process LogAtlas -ErrorAction SilentlyContinue | Where-Object { $_.Path -and $_.Path.StartsWith($root, 'OrdinalIgnoreCase') })
}

function Test-Window($root, $dataDir) {
  for ($i = 0; $i -lt 30; $i++) {
    $w = Get-Window $root | Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1
    if ($w) { Write-Host ("   окно: «{0}»" -f $w.MainWindowTitle) -ForegroundColor Green; return $w }
    Start-Sleep -Seconds 1
  }
  Show-Logs $dataDir
  Fail 'окно LogAtlas не появилось'
}

# служба работает как python.exe из той же папки, окно и его сервер — LogAtlas.exe и pythonw.exe
function Wait-NoProcess($under, $sec = 30, [string[]]$Names = @('python', 'pythonw', 'LogAtlas'), [switch]$SkipService) {
  for ($i = 0; $i -lt $sec; $i++) {
    $procs = @(Get-Process $Names -ErrorAction SilentlyContinue | Where-Object { $_.Path -and $_.Path.StartsWith($under, 'OrdinalIgnoreCase') })
    if ($SkipService -and $procs.Count) {
      # Процессы службы (LogAtlas.exe --service и запущенный им pythonw --service) работают постоянно:
      # их не считаем и смотрим на командную строку.
      $procs = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.Name -in @('python.exe', 'pythonw.exe', 'LogAtlas.exe') -and $_.ExecutablePath -and
        $_.ExecutablePath.StartsWith($under, 'OrdinalIgnoreCase') -and
        -not ($_.CommandLine -and $_.CommandLine -match '--service')
      })
    }
    if (-not $procs.Count) { return }
    Start-Sleep -Seconds 1
  }
  $what = @($procs | ForEach-Object { $id = if ($_.ProcessId) { $_.ProcessId } else { $_.Id }; "$($_.Name)($id)" })
  Fail "процессы LogAtlas из $under не завершились: $($what -join ', ')"
}

# ---------------------------------------------------------------- синтаксис PowerShell-скриптов
# Сборщики журналов и скрипты подготовки хостов не запускаются в этой проверке, но синтаксис их можно
# проверить здесь: ошибка в них иначе дошла бы до людей.
Step 'синтаксис PowerShell-скриптов'
$psFiles = @(
  Get-ChildItem -Path (Join-Path $repo 'scripts'), (Join-Path $repo 'windows'), (Join-Path $repo 'app\setup') `
    -Filter '*.ps1' -File -ErrorAction SilentlyContinue
)
if (-not $psFiles.Count) { Fail 'не найдено ни одного .ps1' }
foreach ($f in $psFiles) {
  $errors = $null
  [void][System.Management.Automation.Language.Parser]::ParseFile($f.FullName, [ref]$null, [ref]$errors)
  if ($errors -and $errors.Count) {
    $errors | Select-Object -First 5 | ForEach-Object {
      Write-Host ("   {0}:{1} {2}" -f $f.Name, $_.Extent.StartLineNumber, $_.Message)
    }
    Fail "синтаксис $($f.Name)"
  }
}
Write-Host "   проверено файлов: $($psFiles.Count)" -ForegroundColor Green

# ---------------------------------------------------------------- переносной zip: окно
Step "переносной zip ($Arch)"
$zip = Join-Path $distRoot "LogAtlas-$Version-portable-$Arch.zip"
Expand-Archive -Path $zip -DestinationPath $work
$proot = Join-Path $work "LogAtlas-$Version-portable-$Arch"
$pdata = Join-Path $proot 'data'
if (-not (Test-Path (Join-Path $proot 'portable.txt'))) { Fail 'в zip нет portable.txt' }
Start-Process -FilePath (Join-Path $proot 'LogAtlas.exe')
$port = Wait-Instance $pdata
$win = Test-Window $proot $pdata
$m = Get-Meta $port
if ($m.runtime -ne 'windows-portable') { Fail "runtime = $($m.runtime)" }
if (-not $m.local_collect) { Fail 'нет «Этот компьютер» в приложении' }
Test-Demo $port 'portable'
$st = Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/status"
if ($st.busy) { Fail 'после разбора панель всё ещё занята' }
# второй запуск не открывает второе окно
Start-Process -FilePath (Join-Path $proot 'LogAtlas.exe')
Start-Sleep -Seconds 4
if ((Get-Window $proot).Count -ne 1) { Fail 'второй запуск открыл второе окно' }
# закрытие окна (как крестиком) завершает сервер и процессы разбора
[void]$win.CloseMainWindow()
Wait-NoProcess $proot
if (Test-Health $port) { Fail 'сервер работает после закрытия окна' }
Write-Host '   окно закрыто, сервер и воркеры разбора завершились' -ForegroundColor Green
Show-Logs $pdata

# ---------------------------------------------------------------- установщик + служба
Step "установщик со службой ($Arch)"
$setup = Join-Path $distRoot "LogAtlas-Setup-$Version-$Arch.exe"
$inst = Join-Path $work 'Program Files\LogAtlas'
$p = Start-Process -FilePath $setup -Wait -PassThru -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART',
  '/ALLUSERS', '/TASKS=service', "/DIR=`"$inst`"", "/LOG=`"$work\setup.log`"")
if ($p.ExitCode -ne 0) { Get-Content "$work\setup.log" -Tail 40 | Write-Host; Fail "установщик: код $($p.ExitCode)" }
if (-not (Get-Service LogAtlas -ErrorAction SilentlyContinue)) { Get-Content "$work\setup.log" -Tail 40 | Write-Host; Fail 'служба не установлена' }
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

# ---------------------------------------------------------------- окно подключается к службе
Step 'окно при работающей службе'
$adata = Join-Path $env:LOCALAPPDATA 'LogAtlas'
Start-Process -FilePath (Join-Path $inst 'LogAtlas.exe')
$win = Test-Window $inst $adata
Start-Sleep -Seconds 3
# Служба сама запускает pythonw.exe из папки программы (--service), поэтому «свой сервер окна» — это
# только pythonw без --service в командной строке.
$appServers = @(Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" -ErrorAction SilentlyContinue |
  Where-Object {
    $_.ExecutablePath -and $_.ExecutablePath.StartsWith($inst, 'OrdinalIgnoreCase') -and
    $_.CommandLine -and ($_.CommandLine -notmatch '--service')
  })
if ($appServers.Count) { Fail 'окно запустило свой сервер, хотя работает служба' }
[void]$win.CloseMainWindow()
Wait-NoProcess $inst -Names pythonw, LogAtlas -SkipService
if (-not (Test-Health 8484)) { Fail 'служба остановилась после закрытия окна' }
Write-Host '   окно открыло службу и закрылось, служба работает' -ForegroundColor Green

# ---------------------------------------------------------------- своё окно (--app) и закрытие по --quit
Step 'окно со своим сервером (--app), закрытие по --quit'
Start-Process -FilePath (Join-Path $inst 'LogAtlas.exe') -ArgumentList '--app'
$port = Wait-Instance $adata
if ($port -eq 8484) { Fail 'окно заняло порт службы' }
[void](Test-Window $inst $adata)
$m = Get-Meta $port
if ($m.runtime -ne 'windows-app') { Fail "runtime окна = $($m.runtime)" }
Start-Process -FilePath (Join-Path $inst 'LogAtlas.exe') -ArgumentList '--quit' -Wait
for ($i = 0; $i -lt 20 -and (Get-Window $inst).Count; $i++) { Start-Sleep -Seconds 1 }
if ((Get-Window $inst).Count) { Show-Logs $adata; Fail 'окно не закрылось по --quit' }
if (-not (Test-Health 8484)) { Fail '--quit остановил службу' }
Write-Host "   окно (порт $port) закрылось по --quit, служба работает" -ForegroundColor Green

# ---------------------------------------------------------------- удаление
Step 'удаление'
Start-Process -FilePath (Join-Path $inst 'unins000.exe') -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART') -Wait
for ($i = 0; $i -lt 90 -and (Test-Path (Join-Path $inst 'unins000.exe')); $i++) { Start-Sleep -Seconds 1 }
if (Get-Service LogAtlas -ErrorAction SilentlyContinue) { Fail 'служба осталась после удаления' }
if (Test-Path (Join-Path $inst 'python')) { Fail 'файлы остались после удаления' }
if (Get-NetFirewallRule -DisplayName 'LogAtlas' -ErrorAction SilentlyContinue) { Fail 'правило брандмауэра осталось' }
Wait-NoProcess $inst

Write-Host "Все проверки пройдены ($Arch)" -ForegroundColor Green

# LogAtlas: сбор журналов Windows (non-GUI режим)
# Запускается от имени администратора через ShellExecute runas
# Параметры передаются через переменные окружения, установленные winlocal.py

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.IO.Compression.FileSystem

# ------------------------------------------------------------------- чтение параметров из окружения
$envOutDir  = [string]$env:LA_OUT_DIR
$envUrl     = [string]$env:LA_URL
$envLogs    = [string]$env:LA_LOGS      # comma-separated list of log names
$envSince   = [string]$env:LA_SINCE     # unix timestamp or empty
$envUntil   = [string]$env:LA_UNTIL     # unix timestamp or empty
$envJobId   = [string]$env:LA_JOB_ID    # unique job id for logging

function SayProgress([int]$current, [int]$total, [string]$label) {
  Write-Output "LOGATLAS-PROGRESS $current $total $label"
}
function SayResult([string]$status, [string]$label, [string]$detail) {
  Write-Output "LOGATLAS-RESULT $status $label|$detail"
}
function SayError([string]$msg) {
  Write-Output "LOGATLAS-RESULT error $msg|$msg"
}

# ------------------------------------------------------------------- проверка прав
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
  [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
  Write-Error "Нужны права администратора"
  exit 1
}

# ------------------------------------------------------------------- валидация параметров
if (-not $envOutDir -or -not (Test-Path $envOutDir)) {
  Write-Error "Не указана выходная папка (LA_OUT_DIR)"
  exit 1
}
if (-not $envLogs) {
  Write-Error "Не указан список журналов (LA_LOGS)"
  exit 1
}

# ------------------------------------------------------------------- определение периода
$query = ''
if ($envSince -and $envUntil) {
  try {
    $sinceSec = [double]$envSince
    $untilSec = [double]$envUntil
    $sinceDt = [datetime]'1970-01-01'.AddSeconds($sinceSec).ToUniversalTime()
    $untilDt = [datetime]'1970-01-01'.AddSeconds($untilSec).AddMilliseconds(-1).ToUniversalTime()
    $fmt = "yyyy-MM-dd'T'HH:mm:ss.fff'Z'"
    $query = "*[System[TimeCreated[@SystemTime>='$($sinceDt.ToString($fmt))' and @SystemTime<='$($untilDt.ToString($fmt))']]]"
  } catch {
    Write-Warning "Не удалось распознать период, собираем всё"
  }
}

# ------------------------------------------------------------------- определение журналов
$logNames = $envLogs -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ }

# Дефолтные журналы (как в GUI-версии)
$defaultLogs = @(
  'Security', 'System', 'Application',
  'Microsoft-Windows-Sysmon/Operational',
  'Microsoft-Windows-PowerShell/Operational',
  'Microsoft-Windows-Windows Defender/Operational',
  'Microsoft-Windows-TaskScheduler/Operational',
  'Microsoft-Windows-TerminalServices-LocalSessionManager/Operational',
  'Microsoft-Windows-TerminalServices-RemoteConnectionManager/Operational'
)

# Если передано '*' — собираем всё
if ($logNames -eq '*') {
  $logNames = $defaultLogs
}

# ------------------------------------------------------------------- проверка доступности журналов
$availableLogs = @()
foreach ($name in $logNames) {
  try {
    $info = & wevtutil.exe gli $name 2>$null
    if ($LASTEXITCODE -eq 0) {
      $availableLogs += $name
    } else {
      SayResult 'skip' $name 'журнал не найден'
    }
  } catch {
    SayResult 'skip' $name 'не удалось проверить'
  }
}

if ($availableLogs.Count -eq 0) {
  SayError 'Ни один из указанных журналов не доступен'
  exit 1
}

# ------------------------------------------------------------------- временная папка
$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$tmpDir = Join-Path $env:TEMP "logatlas_$($envJobId)_$stamp"
try { New-Item -ItemType Directory -Force -Path $tmpDir | Out-Null } catch {
  SayError "Не удалось создать временную папку: $tmpDir"
  exit 1
}

# ------------------------------------------------------------------- архив
$zipName = "${env:COMPUTERNAME}-logs-${stamp}.zip"
$zipPath = Join-Path $envOutDir $zipName
# Если файл уже существует — добавляем uid
if (Test-Path $zipPath) {
  $zipName = "${env:COMPUTERNAME}-logs-${stamp}-$envJobId.zip"
  $zipPath = Join-Path $envOutDir $zipName
}

# ------------------------------------------------------------------- выгрузка журналов
$saved = 0
$errors = @()
$empty = 0
$total = $availableLogs.Count

for ($i = 0; $i -lt $total; $i++) {
  $name = $availableLogs[$i]
  $safeName = ($name -replace '[\\/:"*?<>| ]', '_') + '.evtx'
  $dest = Join-Path $tmpDir $safeName

  SayProgress ($i + 1) $total $name

  try {
    if ($query) {
      & wevtutil.exe epl $name $dest "/q:$query" 2>$null
    } else {
      & wevtutil.exe epl $name $dest 2>$null
    }

    if ($LASTEXITCODE -eq 0 -and (Test-Path $dest)) {
      $len = (Get-Item $dest).Length

      # Подсчитать события в файле
      $count = $null
      try {
        $gi = New-Object System.Diagnostics.ProcessStartInfo
        $gi.FileName = 'wevtutil.exe'
        $gi.Arguments = "gli `"$dest`" /lf:true"
        $gi.UseShellExecute = $false; $gi.CreateNoWindow = $true; $gi.RedirectStandardOutput = $true
        $gp = [System.Diagnostics.Process]::Start($gi)
        $gout = $gp.StandardOutput.ReadToEnd(); $gp.WaitForExit()
        if ($gout -match 'numberOfLogRecords:\s*(\d+)') { $count = [long]$Matches[1] }
      } catch { }

      if ($query -and $count -eq 0) {
        Remove-Item $dest -Force -ErrorAction SilentlyContinue
        $empty++
        SayResult 'empty' $name 'нет событий за период'
      } else {
        $saved++
        $countStr = if ($null -ne $count) { "$count событий" } else { '' }
        SayResult 'ok' $name "$countStr, $([math]::Round($len / 1KB, 1)) КБ"
      }
    } else {
      $errors += $name
      SayResult 'err' $name 'код ошибки: ' + $LASTEXITCODE
    }
  } catch {
    $errors += $name
    SayResult 'err' $name $_.Exception.Message
  }
}

# ------------------------------------------------------------------- проверка результата
if ($saved -eq 0) {
  if ($empty -gt 0) {
    SayError "За выбранный период в журналах нет событий. Выберите период шире."
  } else {
    SayError "Не удалось выгрузить ни одного журнала"
  }
  Remove-Item $tmpDir -Recurse -Force -ErrorAction SilentlyContinue
  exit 1
}

# ------------------------------------------------------------------- упаковка
SayProgress $total $total 'Упаковка'
try {
  [System.IO.Compression.ZipFile]::CreateFromDirectory($tmpDir, $zipPath,
    [System.IO.Compression.CompressionLevel]::Optimal, $false)
  SayResult 'ok' 'zip' "готово, $([math]::Round((Get-Item $zipPath).Length / 1MB, 1)) МБ"
} catch {
  SayError "Не удалось создать архив: $_"
  # Оставляем временные файлы для отладки
  exit 1
}

# ------------------------------------------------------------------- очистка
Remove-Item $tmpDir -Recurse -Force -ErrorAction SilentlyContinue

# ------------------------------------------------------------------- завершение
Write-Output "LOGATLAS-RESULT done zip|$saved журналов, $empty без событий, ошибок: $($errors.Count)"
exit 0

<#
LogAtlas: сбор журналов Windows по SSH для веб-панели LogAtlas.

Запускается принудительной командой sshd (ForceCommand) от имени пользователя logatlas-collect,
которого создаёт скрипт подготовки хоста (панель LogAtlas → «Хосты» → «Подготовка хостов»).
Команда приходит в переменной SSH_ORIGINAL_COMMAND:
  version                                   версия
  scan                                      найденные журналы в JSON
  collect <эпоха_с> <эпоха_по> <id,id,…>     zip с .evtx в stdout; ход сбора — строки LOGATLAS-PROGRESS в stderr

Скрипт только читает журналы. Файл сохранён в UTF-8 с BOM.
#>
$ErrorActionPreference = 'Stop'
$VERSION = '1.0'

# id = канал, название, описание, собирать по умолчанию
$LOGS = [ordered]@{
  'security'    = @('Security', 'Security', 'входы в систему, учётные записи, аудит', $true)
  'system'      = @('System', 'System', 'службы, драйверы, перезагрузки', $true)
  'application' = @('Application', 'Application', 'ошибки и сообщения программ', $true)
  'sysmon'      = @('Microsoft-Windows-Sysmon/Operational', 'Sysmon', 'процессы, сеть, файлы', $true)
  'powershell'  = @('Microsoft-Windows-PowerShell/Operational', 'PowerShell', 'выполненные скрипты', $true)
  'defender'    = @('Microsoft-Windows-Windows Defender/Operational', 'Defender', 'угрозы и настройки защиты', $true)
  'tasks'       = @('Microsoft-Windows-TaskScheduler/Operational', 'Планировщик', 'задания и их запуск', $true)
  'rdp-session' = @('Microsoft-Windows-TerminalServices-LocalSessionManager/Operational', 'RDP: сеансы', 'входы и отключения', $true)
  'rdp-connect' = @('Microsoft-Windows-TerminalServices-RemoteConnectionManager/Operational', 'RDP: подключения', 'попытки соединения', $true)
}

$utf8 = New-Object Text.UTF8Encoding $false
$stdout = [Console]::OpenStandardOutput()
$stderr = [Console]::OpenStandardError()
function Write-Bytes($stream, [string]$text) { $b = $utf8.GetBytes($text); $stream.Write($b, 0, $b.Length); $stream.Flush() }
function Say([string]$t) { Write-Bytes $stderr ($t + "`n") }
function Fail([string]$t, [int]$code = 1) { Say $t; exit $code }

function Invoke-Wev([string]$arguments) {
  $psi = New-Object System.Diagnostics.ProcessStartInfo
  $psi.FileName = 'wevtutil.exe'
  $psi.Arguments = $arguments
  $psi.UseShellExecute = $false
  $psi.CreateNoWindow = $true
  $psi.RedirectStandardOutput = $true
  $psi.RedirectStandardError = $true
  $p = [System.Diagnostics.Process]::Start($psi)
  $errTask = $p.StandardError.ReadToEndAsync()
  $out = $p.StandardOutput.ReadToEnd()
  $p.WaitForExit()
  return @{ Code = $p.ExitCode; Out = $out; Err = $errTask.Result }
}

function Get-EventEpoch([string]$channel, [bool]$newest) {
  $rd = if ($newest) { 'true' } else { 'false' }
  $r = Invoke-Wev ('qe "{0}" /c:1 /rd:{1} /f:xml' -f $channel, $rd)
  if ($r.Code -eq 0 -and $r.Out -match "SystemTime='([^']+)'") {
    try { return [DateTimeOffset]::Parse($Matches[1], [Globalization.CultureInfo]::InvariantCulture).ToUnixTimeSeconds() } catch { }
  }
  return $null
}

function Do-Scan {
  $now = [DateTimeOffset]::Now
  $sources = @()
  foreach ($id in $LOGS.Keys) {
    $d = $LOGS[$id]
    $r = Invoke-Wev ('gli "{0}"' -f $d[0])
    if ($r.Code -ne 0) { continue }
    $records = $null; $size = 0
    if ($r.Out -match 'numberOfLogRecords:\s*(\d+)') { $records = [long]$Matches[1] }
    if ($r.Out -match 'fileSize:\s*(\d+)') { $size = [long]$Matches[1] }
    $first = $null; $last = $null
    if ($records) { $first = Get-EventEpoch $d[0] $false; $last = Get-EventEpoch $d[0] $true }
    $sources += [ordered]@{ id = $id; label = $d[1]; desc = $d[2]; size = $size; records = $records; first = $first; last = $last; default = $d[3] }
  }
  $o = [ordered]@{
    os = 'windows'; host = $env:COMPUTERNAME; version = $VERSION
    tz_offset_min = [int][TimeZoneInfo]::Local.GetUtcOffset($now.DateTime).TotalMinutes
    tz_name = [TimeZoneInfo]::Local.Id; now = $now.ToUnixTimeSeconds(); sources = $sources
  }
  Write-Bytes $stdout (($o | ConvertTo-Json -Depth 4 -Compress) + "`n")
}

function Do-Collect([long]$since, [long]$until, [string[]]$ids) {
  if ($until -le $since) { Fail 'Конец периода раньше начала' }
  $f = [DateTimeOffset]::FromUnixTimeSeconds($since).UtcDateTime.ToString("yyyy-MM-dd'T'HH:mm:ss.000'Z'")
  $t = [DateTimeOffset]::FromUnixTimeSeconds($until).UtcDateTime.ToString("yyyy-MM-dd'T'HH:mm:ss.999'Z'")
  $query = "*[System[TimeCreated[@SystemTime>='$f' and @SystemTime<='$t']]]"
  $tmp = Join-Path ([IO.Path]::GetTempPath()) ('logatlas_' + [guid]::NewGuid().ToString('N'))
  $zip = $tmp + '.zip'
  New-Item -ItemType Directory -Path $tmp -Force | Out-Null
  try {
    $i = 0; $saved = 0
    foreach ($id in $ids) {
      $i++
      $d = $LOGS[$id]
      Say ('LOGATLAS-PROGRESS {0} {1} {2}' -f $i, $ids.Count, $d[1])
      $dest = Join-Path $tmp (($d[0] -replace '[\\/:*?"<>| ]', '_') + '.evtx')
      $r = Invoke-Wev ('epl "{0}" "{1}" "/q:{2}" /ow:true' -f $d[0], $dest, $query)
      if ($r.Code -ne 0) { Say ('  {0}: не удалось выгрузить (код {1}) {2}' -f $d[1], $r.Code, $r.Err.Trim()); continue }
      $g = Invoke-Wev ('gli "{0}" /lf:true' -f $dest)
      $n = $null
      if ($g.Out -match 'numberOfLogRecords:\s*(\d+)') { $n = [long]$Matches[1] }
      if ($n -eq 0) { Remove-Item -LiteralPath $dest -Force; Say ('  {0}: нет событий за период' -f $d[1]); continue }
      $saved++
      Say ('  {0}: {1} событий' -f $d[1], $n)
    }
    if ($saved -eq 0) { Fail 'За выбранный период событий нет' 12 }
    Say 'LOGATLAS-PROGRESS pack'
    Add-Type -AssemblyName System.IO.Compression, System.IO.Compression.FileSystem
    [System.IO.Compression.ZipFile]::CreateFromDirectory($tmp, $zip, [System.IO.Compression.CompressionLevel]::Optimal, $false)
    $fs = [IO.File]::OpenRead($zip)
    try { $fs.CopyTo($stdout); $stdout.Flush() } finally { $fs.Close() }
  } finally {
    Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $zip -Force -ErrorAction SilentlyContinue
  }
}

# ------------------------------------------------------------------ разбор команды
$cmd = [string]$env:SSH_ORIGINAL_COMMAND
if (-not $cmd -and $args.Count) { $cmd = $args -join ' ' }
$parts = @($cmd.Trim() -split '\s+' | Where-Object { $_ })
try {
  switch ($parts[0]) {
    'version' { Write-Bytes $stdout "logatlas-windows $VERSION`n" }
    'scan'    { Do-Scan }
    'collect' {
      if ($parts.Count -ne 4 -or $parts[1] -notmatch '^\d{9,11}$' -or $parts[2] -notmatch '^\d{9,11}$' -or $parts[3] -notmatch '^[a-z0-9,-]+$') {
        Fail 'collect: нужны <эпоха_с> <эпоха_по> <id,id,…>'
      }
      $ids = @($parts[3] -split ',' | Where-Object { $_ })
      foreach ($id in $ids) { if (-not $LOGS.Contains($id)) { Fail "Неизвестный журнал: $id" } }
      Do-Collect ([long]$parts[1]) ([long]$parts[2]) $ids
    }
    default { Fail 'LogAtlas: разрешены только команды version, scan, collect' }
  }
} catch {
  Fail ('Ошибка: ' + $_.Exception.Message)
}
exit 0

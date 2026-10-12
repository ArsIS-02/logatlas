<#
Сбор журналов с Linux-хоста по SSH и отправка в LogAtlas — запуск с Windows.

Двойной щелчок по collect-linux-remote.cmd или в PowerShell:
  powershell -ExecutionPolicy Bypass -File .\collect-linux-remote.ps1 [-HostName redos1] [-User admin] [-Url http://10.0.0.5:8484]

Что происходит:
  1. вы выбираете хост (из файла hosts — отвечающие на ping — или вводите имя/адрес) и пользователя SSH;
  2. скрипт сбора collect-linux.sh передаётся на хост внутри той же SSH-команды и запускается через sudo;
  3. на хосте — диалог: какие журналы собрать и за какой период (время — по часам хоста);
  4. хост упаковывает архив и сам отправляет его в LogAtlas; если не смог — архив скачивается сюда и отправляется отсюда.

Пароли SSH и sudo вводятся в этом окне и никуда не сохраняются. Запоминаются адрес LogAtlas, список
пользователей и последний пользователь для каждого хоста: %APPDATA%\LogAtlas\linux-remote.json.
Файл сохранён в UTF-8 с BOM — так его правильно читает Windows PowerShell 5.1.
#>
param(
  [string]$HostName,
  [string]$User,
  [string]$Url
)

$ErrorActionPreference = 'Continue'
$prevOut = [Console]::OutputEncoding
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }   # вывод хоста приходит в UTF-8

function Say([string]$t = '', [string]$c = 'Gray') { Write-Host $t -ForegroundColor $c }
function Ask([string]$prompt) { Write-Host $prompt -NoNewline -ForegroundColor White; return (Read-Host).Trim() }
function Done([int]$code) {
  try { [Console]::OutputEncoding = $prevOut } catch { }
  exit $code
}

Say ''
Say 'LogAtlas — сбор журналов с Linux-хоста' 'Cyan'
Say ''

# ------------------------------------------------------------------ проверки
$collector = Join-Path $PSScriptRoot 'collect-linux.sh'
if (-not (Test-Path -LiteralPath $collector)) {
  Say "Не найден $collector — он должен лежать рядом с этим скриптом." 'Red'; Done 1
}
foreach ($exe in 'ssh.exe', 'scp.exe') {
  if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) {
    Say "Не найден $exe. Установите «Клиент OpenSSH»: Параметры → Приложения → Дополнительные компоненты." 'Red'; Done 1
  }
}

# ------------------------------------------------------------------ настройки
$cfgDir = Join-Path $env:APPDATA 'LogAtlas'
$cfgFile = Join-Path $cfgDir 'linux-remote.json'
$cfg = @{ Url = ''; Users = @(); Hosts = @{} }
try {
  if (Test-Path -LiteralPath $cfgFile) {
    $j = [IO.File]::ReadAllText($cfgFile) | ConvertFrom-Json
    if ($j.Url) { $cfg.Url = [string]$j.Url }
    if ($j.Users) { $cfg.Users = @($j.Users | ForEach-Object { [string]$_ }) }
    if ($j.Hosts) { foreach ($p in $j.Hosts.PSObject.Properties) { $cfg.Hosts[$p.Name] = [string]$p.Value } }
  }
} catch { }
if (-not $cfg.Url -and $env:LOGATLAS_URL) { $cfg.Url = $env:LOGATLAS_URL }
function Save-Cfg {
  try {
    New-Item -ItemType Directory -Force -Path $cfgDir | Out-Null
    $o = [ordered]@{ Url = $cfg.Url; Users = @($cfg.Users); Hosts = $cfg.Hosts }
    [IO.File]::WriteAllText($cfgFile, ($o | ConvertTo-Json -Depth 4), (New-Object Text.UTF8Encoding $false))
  } catch { }
}

# ------------------------------------------------------------------ адрес LogAtlas
if (-not $Url) {
  $def = $cfg.Url
  $hint = if ($def) { " [$def]" } else { ' (например http://192.168.10.10:8484)' }
  $a = Ask "Адрес LogAtlas$hint, «-» — не отправлять: "
  if ($a -eq '-') { $Url = '' } elseif ($a) { $Url = $a } else { $Url = $def }
}
if ($Url) {
  if ($Url -notmatch '^[a-zA-Z]+://') { $Url = 'http://' + $Url }
  $Url = $Url.TrimEnd('/')
  if ($Url -notmatch '^https?://[A-Za-z0-9.\-\[\]:_/]+$') { Say "Странный адрес LogAtlas: $Url" 'Red'; Done 1 }
  $cfg.Url = $Url
  Save-Cfg
}

# ------------------------------------------------------------------ скрипт сбора для передачи на хост
$text = [IO.File]::ReadAllText($collector) -replace "`r`n", "`n"
$ms = New-Object IO.MemoryStream
$gz = New-Object IO.Compression.GZipStream($ms, [IO.Compression.CompressionMode]::Compress)
$bytes = (New-Object Text.UTF8Encoding $false).GetBytes($text)
$gz.Write($bytes, 0, $bytes.Length)
$gz.Close()
$payload = [Convert]::ToBase64String($ms.ToArray())

# ------------------------------------------------------------------ выбор хоста
function Get-HostsFile {
  $list = @()
  $path = Join-Path $env:SystemRoot 'System32\drivers\etc\hosts'
  if (-not (Test-Path $path)) { return $list }
  foreach ($line in Get-Content $path -ErrorAction SilentlyContinue) {
    $l = ($line -replace '#.*$', '').Trim()
    if (-not $l) { continue }
    $parts = $l -split '\s+'
    if ($parts.Count -lt 2) { continue }
    if ($parts[0] -notmatch '^\d+\.\d+\.\d+\.\d+$' -or $parts[0] -match '^(127\.|0\.)') { continue }
    $list += [pscustomobject]@{ Name = $parts[1]; Addr = $parts[0] }
  }
  return $list
}

function Choose-Host {
  $cands = New-Object System.Collections.Generic.List[object]
  $seen = @{}
  foreach ($h in $cfg.Hosts.Keys) { if (-not $seen[$h]) { $seen[$h] = 1; $cands.Add([pscustomobject]@{ Name = $h; Addr = $h; Ok = $false }) } }
  foreach ($h in Get-HostsFile) {
    if (-not $seen[$h.Name] -and -not $seen[$h.Addr]) { $seen[$h.Name] = 1; $cands.Add([pscustomobject]@{ Name = $h.Name; Addr = $h.Addr; Ok = $false }) }
  }
  if ($cands.Count -gt 0) {
    Say 'Проверяю, какие хосты отвечают на ping…' 'DarkGray'
    $tasks = @()
    foreach ($c in $cands) {
      $t = $null
      try { $t = (New-Object System.Net.NetworkInformation.Ping).SendPingAsync($c.Addr, 800) } catch { }
      $tasks += , @($c, $t)
    }
    $deadline = (Get-Date).AddSeconds(3)
    while ((Get-Date) -lt $deadline -and @($tasks | Where-Object { $_[1] -and -not $_[1].IsCompleted }).Count) { Start-Sleep -Milliseconds 100 }
    foreach ($p in $tasks) {
      $t = $p[1]
      if ($t -and $t.IsCompleted -and -not $t.IsFaulted -and $t.Result.Status -eq 'Success') { $p[0].Ok = $true }
    }
    Say ''
    $i = 0
    foreach ($c in $cands) {
      $i++
      $u = $cfg.Hosts[$c.Name]
      $addr = if ($c.Addr -ne $c.Name) { $c.Addr } else { '' }
      $st = if ($c.Ok) { 'отвечает' } else { 'нет ответа' }
      $line = '  {0,2}. {1,-22} {2,-16} {3,-11} {4}' -f $i, $c.Name, $addr, $st, $(if ($u) { "пользователь: $u" } else { '' })
      Say $line $(if ($c.Ok) { 'Gray' } else { 'DarkGray' })
    }
    Say ''
  }
  while ($true) {
    $a = Ask 'Номер хоста или имя/адрес: '
    if (-not $a) { continue }
    if ($a -match '^\d+$' -and [int]$a -ge 1 -and [int]$a -le $cands.Count) { return $cands[[int]$a - 1].Name }
    if ($a -match '^[A-Za-z0-9._\-:\[\]]+$') { return $a }
    Say "  не понял: $a" 'Yellow'
  }
}

function Choose-User([string]$h) {
  $def = $cfg.Hosts[$h]
  $users = @($cfg.Users)
  if ($users.Count) {
    $i = 0
    $menu = ($users | ForEach-Object { $i++; "$i) $_" }) -join '   '
    Say "  Пользователи: $menu" 'DarkGray'
  }
  $hint = if ($def) { " [$def]" } else { '' }
  while ($true) {
    $a = Ask "Пользователь SSH на $h$hint (номер или имя; нужен sudo): "
    if (-not $a -and $def) { $a = $def }
    if ($a -match '^\d+$' -and [int]$a -ge 1 -and [int]$a -le $users.Count) { $a = $users[[int]$a - 1] }
    if ($a -match '^[a-z_][a-z0-9_.\-]*\$?$') { return $a }
    Say '  имя пользователя: латинские буквы, цифры, «_», «-», «.»' 'Yellow'
  }
}

function Get-Downloads {
  try { $p = (New-Object -ComObject Shell.Application).NameSpace('shell:Downloads').Self.Path; if ($p) { return $p } } catch { }
  return (Join-Path $env:USERPROFILE 'Downloads')
}

function Send-FromHere([string]$h, [string]$u) {
  $dir = Join-Path (Get-Downloads) ('logatlas-' + $h + '-' + (Get-Date -Format 'yyyyMMdd_HHmmss'))
  New-Item -ItemType Directory -Force -Path $dir | Out-Null
  Say ''
  Say "Скачиваю архив с $h в $dir (может снова спросить пароль SSH)…"
  $src = '{0}@{1}:logatlas-collect/last.*' -f $u, $h
  # копируем в текущую папку: путь вида D:\... scp мог бы принять за «хост:путь»
  Push-Location -LiteralPath $dir
  & scp.exe -o StrictHostKeyChecking=accept-new -o NumberOfPasswordPrompts=2 $src .
  if ($LASTEXITCODE -ne 0 -and -not (Test-Path 'last.tar.gz')) {
    & scp.exe -O -o StrictHostKeyChecking=accept-new -o NumberOfPasswordPrompts=1 $src .
  }
  Pop-Location
  $arc = Join-Path $dir 'last.tar.gz'
  if (-not (Test-Path -LiteralPath $arc)) { Say 'Не удалось скачать архив.' 'Red'; return }
  $meta = @{}
  $mf = Join-Path $dir 'last.meta'
  if (Test-Path -LiteralPath $mf) {
    foreach ($l in [IO.File]::ReadAllLines($mf, [Text.Encoding]::UTF8)) { if ($l -match '^(\w+)=(.*)$') { $meta[$Matches[1]] = $Matches[2] } }
    Remove-Item -LiteralPath $mf -Force
  }
  if ($meta.file) { $new = Join-Path $dir $meta.file; Move-Item -LiteralPath $arc -Destination $new -Force; $arc = $new }
  Say ('Архив: {0} ({1:N1} МБ)' -f $arc, ((Get-Item -LiteralPath $arc).Length / 1MB)) 'Green'
  if (-not $Url) {
    Say 'Загрузите его в LogAtlas: «+ Загрузить» → выбрать файл → «Загрузить и разобрать».'
    Start-Process explorer.exe ('/select,"{0}"' -f $arc)
    return
  }
  if (-not (Get-Command curl.exe -ErrorAction SilentlyContinue)) {
    Say 'Нет curl.exe — загрузите архив в LogAtlas вручную.' 'Yellow'; Start-Process explorer.exe ('/select,"{0}"' -f $arc); return
  }
  Say "Отправляю в $Url…"
  $resp = Join-Path $dir 'response.json'
  $tz = if ($meta.tz) { $meta.tz } else { '0' }
  $mode = if ($meta.tz) { 'fixed' } else { 'auto' }
  $name = if ($meta.name) { $meta.name } else { $h }
  # имя дела — через файл: так кириллица и символы «·», «–» дойдут в UTF-8 при любой версии curl.exe
  [IO.File]::WriteAllText((Join-Path $dir 'name.txt'), $name, (New-Object Text.UTF8Encoding $false))
  Push-Location -LiteralPath $dir
  $code = & curl.exe --progress-bar --connect-timeout 8 -o 'response.json' -w '%{http_code}' `
    -F ('files=@{0};type=application/gzip' -f (Split-Path -Leaf $arc)) -F 'name=<name.txt' `
    -F ('tz=' + $tz) -F ('tz_mode=' + $mode) -F 'platform=linux' ($Url + '/api/cases')
  Pop-Location
  Remove-Item -LiteralPath (Join-Path $dir 'name.txt') -Force -ErrorAction SilentlyContinue
  $body = if (Test-Path -LiteralPath $resp) { [IO.File]::ReadAllText($resp) } else { '' }
  Remove-Item -LiteralPath $resp -Force -ErrorAction SilentlyContinue
  if ($code -eq '200' -and $body -match '"id":"([^"]+)"') {
    $link = '{0}/#/c/{1}/overview' -f $Url, $Matches[1]
    Say "Отправлено. Дело разбирается: $link" 'Green'
    if ((Ask 'Открыть в браузере? [Y/n]: ') -notmatch '^(n|н|no|нет)$') { Start-Process $link }
  } else {
    Say "Не удалось отправить (HTTP $code): $body" 'Red'
    Say "Архив остался здесь: $arc — загрузите его в LogAtlas вручную."
    Start-Process explorer.exe ('/select,"{0}"' -f $arc)
  }
}

# ------------------------------------------------------------------ основной цикл
while ($true) {
  $h = if ($HostName) { $HostName } else { Choose-Host }
  $u = if ($User) { $User } else { Choose-User $h }
  $HostName = $null; $User = $null
  $cfg.Hosts[$h] = $u
  if ($cfg.Users -notcontains $u) { $cfg.Users = @($cfg.Users) + $u }
  Save-Cfg

  $opts = '--client'
  if ($Url) { $opts += " --upload '$Url'" }
  $tpl = 'umask 077; f=$(mktemp /tmp/logatlas-collect.XXXXXX) || exit 1; echo {0} | base64 -d | gzip -dc > $f || exit 1; ' +
         'if [ $(id -u) = 0 ]; then bash $f {1}; else sudo bash $f {1}; fi; rc=$?; rm -f $f; exit $rc'
  $remote = $tpl -f $payload, $opts

  Say ''
  Say "Подключаюсь: $u@$h — сначала пароль SSH (если нет ключа), затем пароль sudo." 'Cyan'
  Say ''
  & ssh.exe -t -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 -o ServerAliveInterval=30 -o NumberOfPasswordPrompts=2 "$u@$h" $remote
  $rc = $LASTEXITCODE
  Say ''
  switch ($rc) {
    0 {
      Say 'Готово: архив отправлен в LogAtlas — ссылка на дело выше.' 'Green'
      if ($Url -and (Ask 'Открыть LogAtlas в браузере? [Y/n]: ') -notmatch '^(n|н|no|нет)$') { Start-Process $Url }
    }
    10 {
      if ($Url) { Say 'Архив собран, но с хоста отправить его не удалось.' 'Yellow' } else { Say 'Архив собран.' 'Green' }
      $q = 'Скачать архив на этот компьютер{0}? [Y/n]: ' -f $(if ($Url) { ' и отправить отсюда' } else { '' })
      if ((Ask $q) -notmatch '^(n|н|no|нет)$') {
        Send-FromHere $h $u
      } else {
        Say "Архив лежит на хосте: ~$u/logatlas-collect/last.tar.gz"
      }
    }
    11 { Say 'Сбор отменён.' 'Yellow' }
    12 { Say 'За выбранный период записей нет — архив не создан.' 'Yellow' }
    255 {
      Say "Не удалось подключиться к $h по SSH." 'Red'
      Say '  • неверный пароль или пользователь;'
      Say '  • хост выключен или порт 22 закрыт;'
      Say '  • ваш компьютер заблокирован fail2ban после неудачных попыток — на хосте: sudo fail2ban-client unban <ваш IP>;'
      Say "  • сменился ключ хоста (переустановка) — убедитесь, что это ваш хост, и выполните: ssh-keygen -R $h"
    }
    default { Say "Сбор завершился с ошибкой (код $rc). Подробности — в выводе выше (часто: у пользователя нет прав sudo)." 'Red' }
  }
  Say ''
  if ((Ask 'Собрать журналы с другого хоста? [y/N]: ') -notmatch '^(y|д|yes|да)$') { break }
  Say ''
}
Done 0

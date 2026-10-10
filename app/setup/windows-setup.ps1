# LogAtlas: подготовка Windows-хоста к сбору журналов из веб-панели @@BASE@@
#
# В PowerShell, открытом от имени администратора, в папке со скачанным файлом:
#   powershell -ExecutionPolicy Bypass -File .\logatlas-setup.ps1               ключ панели — к вашей учётной записи
#   powershell -ExecutionPolicy Bypass -File .\logatlas-setup.ps1 -CreateUser   вместо этого создать пользователя logatlas-collect
#   powershell -ExecutionPolicy Bypass -File .\logatlas-setup.ps1 -Remove       убрать всё, что сделал скрипт
#
# Что делает:
#   • включает встроенный OpenSSH Server (служба sshd, автозапуск);
#   • кладёт скрипт сбора в C:\ProgramData\LogAtlas (только читает журналы);
#   • добавляет ключ панели к вашей учётной записи с ограничениями: вход только с адреса панели,
#     без терминала и переадресаций, единственная команда — скрипт сбора. Новых пользователей не создаёт.
#     Пароль вашей учётной записи и ваши собственные ключи SSH не затрагиваются.
#   • с -CreateUser — вместо этого создаёт локального пользователя logatlas-collect со случайным паролем,
#     без прав администратора, в группе «Читатели журнала событий».
param([switch]$Remove, [switch]$CreateUser)

$ErrorActionPreference = 'Stop'
$PanelHost = '@@PANEL_HOST@@'
$PubKey = '@@PUBKEY@@'
$Dir = Join-Path $env:ProgramData 'LogAtlas'
$Cfg = Join-Path $env:ProgramData 'ssh\sshd_config'
$AdminKeys = Join-Path $env:ProgramData 'ssh\administrators_authorized_keys'
$Collector = Join-Path $Dir 'collect-windows-remote.ps1'
$NewUser = 'logatlas-collect'
$Begin = '# LogAtlas begin'
$End = '# LogAtlas end'
$Marker = 'logatlas-panel'
$KeyBody = ($PubKey -split '\s+')[1]

function Say([string]$t, [string]$c = 'Gray') { Write-Host $t -ForegroundColor $c }

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { Say 'Запустите PowerShell от имени администратора.' 'Red'; return }

$Me = $env:USERNAME
$MyKeys = Join-Path $env:USERPROFILE '.ssh\authorized_keys'
# для администраторов Windows OpenSSH берёт ключи из administrators_authorized_keys (блок «Match Group administrators»)
function Get-MyKeyFile {
  if ((Test-Path $Cfg) -and ([IO.File]::ReadAllText($Cfg) -match '(?m)^\s*Match\s+Group\s+administrators')) { return $AdminKeys }
  return $MyKeys
}

function Remove-Block {
  if (Test-Path $Cfg) {
    $t = [IO.File]::ReadAllText($Cfg)
    $t2 = [regex]::Replace($t, "(?s)\r?\n?$([regex]::Escape($Begin)).*?$([regex]::Escape($End))\r?\n?", "`r`n")
    if ($t2 -ne $t) { [IO.File]::WriteAllText($Cfg, $t2.TrimEnd() + "`r`n", (New-Object Text.ASCIIEncoding)) }
  }
}

function Remove-KeyLine([string]$file) {
  if (-not (Test-Path $file)) { return }
  $lines = [IO.File]::ReadAllLines($file)
  $keep = @($lines | Where-Object { $_ -notmatch [regex]::Escape($Marker) -and -not ($KeyBody -and $_.Contains($KeyBody)) })
  if ($keep.Count -ne $lines.Count) { [IO.File]::WriteAllLines($file, $keep, (New-Object Text.ASCIIEncoding)) }
}

function Set-AdminKeysAcl([string]$file) {
  # так требует sshd: только SYSTEM и администраторы
  & icacls.exe $file /inheritance:r /grant '*S-1-5-18:F' '*S-1-5-32-544:F' | Out-Null
}

if ($Remove) {
  Say 'Удаляю настройки LogAtlas…'
  Remove-Block
  Remove-KeyLine $AdminKeys
  Remove-KeyLine $MyKeys
  if (Get-LocalUser -Name $NewUser -ErrorAction SilentlyContinue) { Remove-LocalUser -Name $NewUser }
  Remove-Item -Recurse -Force $Dir -ErrorAction SilentlyContinue
  Get-NetFirewallRule -Name 'LogAtlas-SSH' -ErrorAction SilentlyContinue | Remove-NetFirewallRule
  Restart-Service sshd -ErrorAction SilentlyContinue
  Say 'Готово. OpenSSH Server оставлен включённым; выключить: Stop-Service sshd; Set-Service sshd -StartupType Disabled' 'Green'
  return
}

# ------------------------------------------------------------------ адрес панели
$PanelIp = $PanelHost
if ($PanelHost -notmatch '^\d+\.\d+\.\d+\.\d+$') {
  try { $PanelIp = ([Net.Dns]::GetHostAddresses($PanelHost) | Where-Object { $_.AddressFamily -eq 'InterNetwork' } | Select-Object -First 1).IPAddressToString } catch { $PanelIp = $null }
  if (-not $PanelIp) { Say "Не удалось узнать IP панели $PanelHost." 'Red'; return }
}
$From = "$PanelIp,172.16.0.0/12,127.0.0.1"

# ------------------------------------------------------------------ OpenSSH Server
Say 'OpenSSH Server…'
$cap = Get-WindowsCapability -Online -Name 'OpenSSH.Server*' | Select-Object -First 1
if ($cap -and $cap.State -ne 'Installed') {
  Say '  устанавливаю компонент (может занять несколько минут)…'
  Add-WindowsCapability -Online -Name $cap.Name | Out-Null
}
Set-Service -Name sshd -StartupType Automatic
Start-Service sshd
if (-not (Test-Path $Cfg)) { Start-Sleep 2 }
if (-not (Test-Path $Cfg)) { Say "Не найден $Cfg — служба sshd не запустилась?" 'Red'; return }
if (-not (Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction SilentlyContinue | Where-Object Enabled -eq 'True')) {
  if (-not (Get-NetFirewallRule -Name 'LogAtlas-SSH' -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -Name 'LogAtlas-SSH' -DisplayName 'LogAtlas: SSH с панели' -Direction Inbound -Protocol TCP -LocalPort 22 -RemoteAddress $PanelIp -Action Allow | Out-Null
  }
}

# ------------------------------------------------------------------ скрипт сбора
New-Item -ItemType Directory -Force -Path $Dir | Out-Null
# текст скрипта сбора — тот же, что scripts/collect-windows-remote.ps1 в репозитории LogAtlas
$collectorText = @'
@@COLLECT_WINDOWS_TEXT@@
'@
[IO.File]::WriteAllText($Collector, $collectorText, (New-Object Text.UTF8Encoding $true))
$force = "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $Collector"

# ------------------------------------------------------------------ ключ панели
$backup = "$Cfg.logatlas-backup"
Copy-Item $Cfg $backup -Force
Remove-Block

if ($CreateUser) {
  $User = $NewUser
  Say "Пользователь $User…"
  $chars = [char[]]'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789!#%+-='
  $pw = -join (1..28 | ForEach-Object { $chars | Get-Random })
  $sec = ConvertTo-SecureString ($pw + 'aA1!') -AsPlainText -Force
  if (Get-LocalUser -Name $User -ErrorAction SilentlyContinue) {
    Set-LocalUser -Name $User -Password $sec -PasswordNeverExpires $true
  } else {
    New-LocalUser -Name $User -Password $sec -PasswordNeverExpires -UserMayNotChangePassword -AccountNeverExpires -Description 'LogAtlas: сбор журналов' | Out-Null
  }
  try { Add-LocalGroupMember -SID 'S-1-5-32-573' -Member $User -ErrorAction Stop } catch { if ($_.FullyQualifiedErrorId -notmatch 'MemberExists') { throw } }
  & icacls.exe $Dir /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' "${User}:(OI)(CI)RX" | Out-Null
  & icacls.exe $Dir /setowner '*S-1-5-32-544' /T | Out-Null
  [IO.File]::WriteAllText((Join-Path $Dir 'authorized_keys'), ('restrict,from="{0}" {1} {2}' -f $From, $PubKey, $Marker) + "`n", (New-Object Text.ASCIIEncoding))
  $block = @('', $Begin, "Match User $User",
    '    AuthorizedKeysFile __PROGRAMDATA__/LogAtlas/authorized_keys',
    "    ForceCommand $force",
    '    PermitTTY no', '    AllowTcpForwarding no', '    X11Forwarding no', '    PermitTunnel no', $End, '') -join "`r`n"
  [IO.File]::AppendAllText($Cfg, $block, (New-Object Text.ASCIIEncoding))
} else {
  $User = $Me
  $keyFile = Get-MyKeyFile
  Say "Ключ панели — к учётной записи $User ($keyFile)…"
  # каталог со скриптом: изменять могут только SYSTEM и администраторы, читать — все
  & icacls.exe $Dir /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' '*S-1-5-32-545:(OI)(CI)RX' | Out-Null
  New-Item -ItemType Directory -Force -Path (Split-Path $keyFile) | Out-Null
  Remove-KeyLine $keyFile
  # restrict — без терминала и переадресаций; command — единственная разрешённая команда; from — только с панели
  $line = 'restrict,from="{0}",command="{1}" {2} {3}' -f $From, $force, $PubKey, $Marker
  $existing = if (Test-Path $keyFile) { [IO.File]::ReadAllText($keyFile) } else { '' }
  if ($existing -and -not $existing.EndsWith("`n")) { $existing += "`r`n" }
  [IO.File]::WriteAllText($keyFile, $existing + $line + "`r`n", (New-Object Text.ASCIIEncoding))
  if ($keyFile -eq $AdminKeys) { Set-AdminKeysAcl $keyFile }
}

$sshd = Join-Path $env:WINDIR 'System32\OpenSSH\sshd.exe'
& $sshd -t 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
  Copy-Item $backup $Cfg -Force
  Say 'sshd отверг конфигурацию — изменения sshd_config отменены. Проверьте вручную: sshd.exe -t' 'Red'
  return
}
Restart-Service sshd

# ------------------------------------------------------------------ проверка
$env:SSH_ORIGINAL_COMMAND = 'version'
$v = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Collector 2>&1
Remove-Item Env:\SSH_ORIGINAL_COMMAND
Say ''
Say ("Готово. {0} подготовлен ({1}): вход только с {2} по ключу панели LogAtlas, только для сбора журналов." -f $env:COMPUTERNAME, ($v -join ' '), $PanelIp) 'Green'
Say ("В панели: «Хосты» → «+ Хост» → адрес этого компьютера, система Windows, пользователь: {0}" -f $User) 'Cyan'

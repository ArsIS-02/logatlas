# LogAtlas: подготовка Windows-хоста к сбору журналов из веб-панели.
#
# В PowerShell, открытом от имени администратора:
#   iex (iwr -UseBasicParsing @@BASE@@/setup/windows.ps1).Content
# Убрать всё, что сделал скрипт:
#   $env:LOGATLAS_REMOVE = 1; iex (iwr -UseBasicParsing @@BASE@@/setup/windows.ps1).Content
#
# Что делает:
#   • включает встроенный OpenSSH Server (служба sshd, автозапуск);
#   • создаёт локального пользователя logatlas-collect со случайным паролем, без прав администратора,
#     в группе «Читатели журнала событий» (Event Log Readers);
#   • кладёт ключ панели в C:\ProgramData\LogAtlas\authorized_keys: вход только с адреса панели;
#   • в sshd_config для этого пользователя — единственная команда: скрипт сбора (только читает журналы).

& {
$ErrorActionPreference = 'Stop'
$PanelHost = '@@PANEL_HOST@@'
$PubKey = '@@PUBKEY@@'
$User = 'logatlas-collect'
$Dir = Join-Path $env:ProgramData 'LogAtlas'
$Cfg = Join-Path $env:ProgramData 'ssh\sshd_config'
$Begin = '# LogAtlas begin'
$End = '# LogAtlas end'

function Say([string]$t, [string]$c = 'Gray') { Write-Host $t -ForegroundColor $c }

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { Say 'Запустите PowerShell от имени администратора.' 'Red'; return }

function Remove-Block {
  if (Test-Path $Cfg) {
    $t = [IO.File]::ReadAllText($Cfg)
    $t2 = [regex]::Replace($t, "(?s)\r?\n?$([regex]::Escape($Begin)).*?$([regex]::Escape($End))\r?\n?", "`r`n")
    if ($t2 -ne $t) { [IO.File]::WriteAllText($Cfg, $t2.TrimEnd() + "`r`n", (New-Object Text.ASCIIEncoding)) }
  }
}

if ($env:LOGATLAS_REMOVE) {
  Remove-Item Env:\LOGATLAS_REMOVE
  Say 'Удаляю настройки LogAtlas…'
  Remove-Block
  if (Get-LocalUser -Name $User -ErrorAction SilentlyContinue) { Remove-LocalUser -Name $User }
  Remove-Item -Recurse -Force $Dir -ErrorAction SilentlyContinue
  Get-NetFirewallRule -Name 'LogAtlas-SSH' -ErrorAction SilentlyContinue | Remove-NetFirewallRule
  Restart-Service sshd -ErrorAction SilentlyContinue
  Say 'Готово. OpenSSH Server оставлен включённым.' 'Green'
  return
}

# ------------------------------------------------------------------ адрес панели
$PanelIp = $PanelHost
if ($PanelHost -notmatch '^\d+\.\d+\.\d+\.\d+$') {
  try { $PanelIp = ([Net.Dns]::GetHostAddresses($PanelHost) | Where-Object { $_.AddressFamily -eq 'InterNetwork' } | Select-Object -First 1).IPAddressToString } catch { $PanelIp = $null }
  if (-not $PanelIp) { Say "Не удалось узнать IP панели $PanelHost." 'Red'; return }
}

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

# ------------------------------------------------------------------ пользователь
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

# ------------------------------------------------------------------ скрипт сбора и ключ
New-Item -ItemType Directory -Force -Path $Dir | Out-Null
$collector = Join-Path $Dir 'collect-windows-remote.ps1'
$bytes = [Convert]::FromBase64String('@@COLLECT_WINDOWS_B64@@')
[IO.File]::WriteAllBytes($collector, $bytes)
$keys = Join-Path $Dir 'authorized_keys'
[IO.File]::WriteAllText($keys, ('restrict,from="{0},172.16.0.0/12,127.0.0.1" {1}' -f $PanelIp, $PubKey) + "`n", (New-Object Text.ASCIIEncoding))
# права: SYSTEM и администраторы — полный доступ, logatlas-collect — чтение
& icacls.exe $Dir /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' "${User}:(OI)(CI)RX" | Out-Null
& icacls.exe $Dir /setowner '*S-1-5-32-544' /T | Out-Null

# ------------------------------------------------------------------ sshd_config
Say 'sshd_config…'
$backup = "$Cfg.logatlas-backup"
Copy-Item $Cfg $backup -Force
Remove-Block
$keysCfg = '__PROGRAMDATA__/LogAtlas/authorized_keys'
$block = @(
  '', $Begin,
  "Match User $User",
  "    AuthorizedKeysFile $keysCfg",
  ('    ForceCommand powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}"' -f $collector),
  '    PermitTTY no', '    AllowTcpForwarding no', '    X11Forwarding no', '    PermitTunnel no',
  $End, ''
) -join "`r`n"
[IO.File]::AppendAllText($Cfg, $block, (New-Object Text.ASCIIEncoding))
$sshd = Join-Path $env:WINDIR 'System32\OpenSSH\sshd.exe'
& $sshd -t 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
  Copy-Item $backup $Cfg -Force
  Say 'sshd отверг конфигурацию — изменения отменены. Проверьте вручную: sshd.exe -t' 'Red'
  return
}
Restart-Service sshd

# ------------------------------------------------------------------ проверка
$env:SSH_ORIGINAL_COMMAND = 'version'
$v = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $collector 2>&1
Remove-Item Env:\SSH_ORIGINAL_COMMAND
Say ''
Say ("Готово. {0} подготовлен: пользователь {1}, вход только с {2} по ключу панели LogAtlas ({3})." -f $env:COMPUTERNAME, $User, $PanelIp, ($v -join ' ')) 'Green'
Say 'Добавьте хост в панели: «Хосты» → «+ Хост» (адрес этого компьютера, система: Windows).'
}

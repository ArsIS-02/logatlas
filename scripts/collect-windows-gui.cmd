@echo off
rem LogAtlas: collect Windows event logs with a progress window. Just double-click this file.
rem Windows asks for administrator rights: they are needed to read the Security log.
rem The script only reads logs: nothing is changed and nothing is sent anywhere.
rem
rem LogAtlas address for the "Open" button (optional), e.g. http://192.168.10.10:8484
set "LOGATLAS_URL="
rem
rem Troubleshooting: run "collect-windows-gui.cmd debug" from a console to keep the PowerShell window visible.
setlocal
set "LA_SELF=%~f0"
set "LA_WIN=Hidden"
set "LA_NOEXIT="
if /i "%~1"=="debug" (
  set "LA_WIN=Normal"
  set "LA_NOEXIT=-NoExit"
)
fltmc >nul 2>&1 || (
  if /i "%~1"=="elevated" goto run
  powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command "try { Start-Process -FilePath $env:LA_SELF -ArgumentList 'elevated' -Verb RunAs } catch { }"
  exit /b
)
:run
start "" powershell -NoProfile -STA -ExecutionPolicy Bypass -WindowStyle %LA_WIN% %LA_NOEXIT% -Command "try { $t = [IO.File]::ReadAllText($env:LA_SELF, [Text.Encoding]::UTF8); & ([scriptblock]::Create($t.Substring($t.IndexOf('#' + '##PS')))) } catch { Add-Type -AssemblyName System.Windows.Forms; [void][Windows.Forms.MessageBox]::Show($_.ToString(), 'LogAtlas') }"
exit /b

###PS
# LogAtlas: сбор журналов Windows с окном хода работы.
# Этот блок запускает collect-windows-gui.cmd; отдельно его запускать не нужно.
$ErrorActionPreference = 'Continue'
Add-Type -AssemblyName System.Windows.Forms, System.Drawing, System.IO.Compression, System.IO.Compression.FileSystem
try {
  Add-Type -Namespace LogAtlas -Name Dpi -MemberDefinition '[DllImport("user32.dll")] public static extern bool SetProcessDPIAware();'
  [void][LogAtlas.Dpi]::SetProcessDPIAware()
} catch { }
[System.Windows.Forms.Application]::EnableVisualStyles()

$AppTitle = 'LogAtlas — сбор журналов Windows'
function Show-Msg([string]$Text, [string]$Icon = 'Information') {
  [void][System.Windows.Forms.MessageBox]::Show($Text, $AppTitle, 'OK', $Icon)
}

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
  [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
  Show-Msg ("Нужны права администратора — без них журнал Security недоступен.`n`n" +
            "Запустите collect-windows-gui.cmd двойным щелчком и разрешите запрос Windows «Разрешить этому приложению вносить изменения»: " +
            "скрипт только читает журналы.") 'Warning'
  return
}

# ---------------------------------------------------------------- настройки
$cfgDir = Join-Path $env:APPDATA 'LogAtlas'
$cfgFile = Join-Path $cfgDir 'collect-windows.json'
$cfg = @{ OutDir = [Environment]::GetFolderPath('Desktop'); Url = [string]$env:LOGATLAS_URL; Period = 1 }
try {
  if (Test-Path -LiteralPath $cfgFile) {
    $j = [IO.File]::ReadAllText($cfgFile) | ConvertFrom-Json
    if ($j.OutDir -and (Test-Path -LiteralPath $j.OutDir)) { $cfg.OutDir = [string]$j.OutDir }
    if ($j.Url) { $cfg.Url = [string]$j.Url }
    if ($null -ne $j.Period -and [int]$j.Period -ge 0 -and [int]$j.Period -le 5) { $cfg.Period = [int]$j.Period }
  }
} catch { }
# Запуск из приложения LogAtlas для Windows («Собрать журналы этого компьютера»): архив кладётся
# в папку, которую приложение забирает на разбор само, а настройки сборщика не меняются.
$LaDesktop = [bool]$env:LA_OUT
if ($LaDesktop) {
  $cfg.OutDir = [string]$env:LA_OUT
  if ($env:LA_URL) { $cfg.Url = [string]$env:LA_URL }
}
function Save-Cfg {
  if ($LaDesktop) { return }
  try {
    New-Item -ItemType Directory -Force -Path $cfgDir | Out-Null
    [IO.File]::WriteAllText($cfgFile, (@{ OutDir = $cfg.OutDir; Url = $cfg.Url; Period = $cfg.Period } | ConvertTo-Json))
  } catch { }
}

function Format-Size([double]$b) {
  if ($b -ge 1GB) { return '{0:N1} ГБ' -f ($b / 1GB) }
  if ($b -ge 1MB) { return '{0:N1} МБ' -f ($b / 1MB) }
  if ($b -gt 0)   { return '{0:N0} КБ' -f [math]::Max(1, $b / 1KB) }
  return '—'
}

# ---------------------------------------------------------------- журналы
$defs = @(
  @{ Name = 'Security';    Label = 'Security';    Desc = 'входы в систему, учётные записи, аудит' },
  @{ Name = 'System';      Label = 'System';      Desc = 'службы, драйверы, перезагрузки' },
  @{ Name = 'Application'; Label = 'Application'; Desc = 'ошибки и сообщения программ' },
  @{ Name = 'Microsoft-Windows-Sysmon/Operational'; Label = 'Sysmon'; Desc = 'процессы, сеть, файлы' },
  @{ Name = 'Microsoft-Windows-PowerShell/Operational'; Label = 'PowerShell'; Desc = 'выполненные скрипты' },
  @{ Name = 'Microsoft-Windows-Windows Defender/Operational'; Label = 'Defender'; Desc = 'угрозы и настройки защиты' },
  @{ Name = 'Microsoft-Windows-TaskScheduler/Operational'; Label = 'Планировщик'; Desc = 'задания и их запуск' },
  @{ Name = 'Microsoft-Windows-TerminalServices-LocalSessionManager/Operational'; Label = 'RDP: сеансы'; Desc = 'входы и отключения' },
  @{ Name = 'Microsoft-Windows-TerminalServices-RemoteConnectionManager/Operational'; Label = 'RDP: подключения'; Desc = 'попытки соединения' }
)
$logs = foreach ($d in $defs) {
  $info = & wevtutil.exe gli $d.Name 2>$null
  $exists = ($LASTEXITCODE -eq 0)
  $records = $null; $size = 0
  if ($exists) {
    foreach ($line in $info) {
      if ($line -match '^\s*numberOfLogRecords:\s*(\d+)') { $records = [long]$Matches[1] }
      elseif ($line -match '^\s*fileSize:\s*(\d+)') { $size = [long]$Matches[1] }
    }
  }
  [pscustomobject]@{ Name = $d.Name; Label = $d.Label; Desc = $d.Desc; Exists = $exists; Records = $records; Size = $size }
}

$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$tmp = Join-Path $env:TEMP ('logatlas_' + $stamp)

# Состояние окна и фоновой задачи (хэш-таблицы — чтобы обработчики событий меняли общие значения)
$st = @{ Phase = 'intro'; Items = @(); Zip = ''; Sw = New-Object System.Diagnostics.Stopwatch; Ps = $null; Handle = $null }
$sync = [hashtable]::Synchronized(@{
  Phase = 'idle'; Index = -1; Done = 0; Saved = 0; Bytes = 0; Events = 0; ZipSize = 0; Error = ''; Cancel = $false; Proc = $null
  Status = [hashtable]::Synchronized(@{}); State = [hashtable]::Synchronized(@{})
})

# ---------------------------------------------------------------- фоновая выгрузка
# WORKER-BEGIN
$worker = {
  param($sync, [string[]]$names, [string]$tmp, [string]$zip, [string]$query)
  function Format-Size([double]$b) {
    if ($b -ge 1GB) { return '{0:N1} ГБ' -f ($b / 1GB) }
    if ($b -ge 1MB) { return '{0:N1} МБ' -f ($b / 1MB) }
    if ($b -gt 0)   { return '{0:N0} КБ' -f [math]::Max(1, $b / 1KB) }
    return '—'
  }
  $final = 'error'
  try {
    $oem = [Text.Encoding]::GetEncoding([Globalization.CultureInfo]::CurrentCulture.TextInfo.OEMCodePage)
    for ($k = 0; $k -lt $names.Count; $k++) {
      if ($sync.Cancel) { break }
      $sync.Index = $k
      $sync.State[$k] = 'run'
      $sync.Status[$k] = 'выгружается…'
      $sync.Phase = 'export'
      $dest = Join-Path $tmp (($names[$k] -replace '[\\/:*?"<>| ]', '_') + '.evtx')
      $psi = New-Object System.Diagnostics.ProcessStartInfo
      $psi.FileName = 'wevtutil.exe'
      if ($query) { $psi.Arguments = 'epl "{0}" "{1}" "/q:{2}" /ow:true' -f $names[$k], $dest, $query }
      else { $psi.Arguments = 'epl "{0}" "{1}" /ow:true' -f $names[$k], $dest }
      $psi.UseShellExecute = $false
      $psi.CreateNoWindow = $true
      $psi.RedirectStandardOutput = $true
      $psi.RedirectStandardError = $true
      $psi.StandardOutputEncoding = $oem
      $psi.StandardErrorEncoding = $oem
      $p = [System.Diagnostics.Process]::Start($psi)
      $sync.Proc = $p
      $outTask = $p.StandardOutput.ReadToEndAsync()
      $err = $p.StandardError.ReadToEnd()
      $p.WaitForExit()
      $sync.Proc = $null
      if ($sync.Cancel) { break }
      if ($p.ExitCode -eq 0 -and (Test-Path -LiteralPath $dest)) {
        $len = (Get-Item -LiteralPath $dest).Length
        # сколько событий попало в файл
        $count = $null
        try {
          $gi = New-Object System.Diagnostics.ProcessStartInfo
          $gi.FileName = 'wevtutil.exe'
          $gi.Arguments = 'gli "{0}" /lf:true' -f $dest
          $gi.UseShellExecute = $false; $gi.CreateNoWindow = $true; $gi.RedirectStandardOutput = $true
          $gp = [System.Diagnostics.Process]::Start($gi)
          $gout = $gp.StandardOutput.ReadToEnd(); $gp.WaitForExit()
          if ($gout -match 'numberOfLogRecords:\s*(\d+)') { $count = [long]$Matches[1] }
        } catch { }
        if ($query -and $count -eq 0) {
          Remove-Item -LiteralPath $dest -Force -ErrorAction SilentlyContinue
          $sync.State[$k] = 'empty'
          $sync.Status[$k] = 'нет событий за период'
        } else {
          $sync.Bytes += $len
          $sync.Saved++
          $sync.Events += $(if ($count) { $count } else { 0 })
          $sync.State[$k] = 'ok'
          $sync.Status[$k] = 'готово · ' + $(if ($null -ne $count) { $count.ToString('N0') + ' соб. · ' } else { '' }) + (Format-Size $len)
        }
      } else {
        $msg = (($err + ' ' + $outTask.Result) -replace '\s+', ' ').Trim()
        if (-not $msg) { $msg = 'код ' + $p.ExitCode }
        $sync.State[$k] = 'err'
        $sync.Status[$k] = 'ошибка: ' + $msg
      }
      $sync.Done = $k + 1
    }
    if ($sync.Cancel) { $final = 'cancelled'; return }
    if ($sync.Saved -eq 0) {
      if (@($sync.State.Values | Where-Object { $_ -eq 'empty' }).Count) { $sync.Error = 'За выбранный период в журналах нет событий. Выберите период шире.' }
      else { $sync.Error = 'Не удалось выгрузить ни одного журнала.' }
      return
    }
    $sync.Phase = 'zip'
    [System.IO.Compression.ZipFile]::CreateFromDirectory($tmp, $zip, [System.IO.Compression.CompressionLevel]::Optimal, $false)
    $sync.ZipSize = (Get-Item -LiteralPath $zip).Length
    $final = 'done'
  } catch {
    $sync.Error = $_.Exception.Message
  } finally {
    if ($final -ne 'error' -or $sync.Saved -eq 0) { Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue }
    $sync.Phase = $final
  }
}
# WORKER-END

# ---------------------------------------------------------------- окно
function New-Ctl([string]$Type, [int]$X, [int]$Y, [int]$W, [int]$H, [string]$Text = '') {
  $c = New-Object ("System.Windows.Forms." + $Type)
  $c.Location = New-Object System.Drawing.Point($X, $Y)
  $c.Size = New-Object System.Drawing.Size($W, $H)
  if ($Text) { $c.Text = $Text }
  return $c
}
$gray = [System.Drawing.Color]::FromArgb(96, 96, 96)

$form = New-Object System.Windows.Forms.Form
$form.SuspendLayout()
$form.AutoScaleDimensions = New-Object System.Drawing.SizeF(96, 96)
$form.AutoScaleMode = 'Dpi'
$form.Text = $AppTitle
$form.ClientSize = New-Object System.Drawing.Size(760, 640)
$form.StartPosition = 'CenterScreen'
$form.FormBorderStyle = 'FixedDialog'
$form.MaximizeBox = $false
$form.Font = New-Object System.Drawing.Font('Segoe UI', 9.75)
try { $form.Icon = [System.Drawing.Icon]::ExtractAssociatedIcon((Join-Path $env:SystemRoot 'System32\eventvwr.exe')) } catch { }

$lblTitle = New-Ctl Label 20 14 720 30 'Сбор журналов Windows для LogAtlas'
$lblTitle.Font = New-Object System.Drawing.Font('Segoe UI Semibold', 13)
$lblInfo = New-Ctl Label 20 48 720 60 ((("Журналы событий этого компьютера ({0}) будут выгружены в файлы .evtx и упакованы в один zip-архив для загрузки в LogAtlas. " -f $env:COMPUTERNAME)) +
  "Скрипт только читает журналы: в системе ничего не меняется, данные никуда не отправляются. Снимите галочки с журналов, которые не нужны.")

$lv = New-Ctl ListView 20 112 720 250
$lv.View = 'Details'
$lv.CheckBoxes = $true
$lv.FullRowSelect = $true
$lv.HeaderStyle = 'Nonclickable'
$lv.ShowItemToolTips = $true
[void]$lv.Columns.Add('Журнал', 330)
[void]$lv.Columns.Add('Событий', 90, 'Right')
[void]$lv.Columns.Add('Размер', 90, 'Right')
[void]$lv.Columns.Add('Статус', 186)

$lblSum = New-Ctl Label 20 368 720 22
$lblPeriod = New-Ctl Label 20 402 64 20 'Период:'
$cmbPeriod = New-Ctl ComboBox 86 398 190 26
$cmbPeriod.DropDownStyle = 'DropDownList'
[void]$cmbPeriod.Items.AddRange([object[]]@('Последние 24 часа', 'Последние 3 дня', 'Последние 7 дней', 'Последние 30 дней', 'Всё время', 'Свой период'))
$dtFrom = New-Ctl DateTimePicker 288 398 160 26
$lblDash = New-Ctl Label 452 402 18 20 '—'
$dtTo = New-Ctl DateTimePicker 472 398 160 26
foreach ($d in @($dtFrom, $dtTo)) { $d.Format = 'Custom'; $d.CustomFormat = 'dd.MM.yyyy HH:mm' }
$lblTz = New-Ctl Label 640 402 100 20 ('UTC' + (Get-Date -Format 'zzz'))
$lblTz.ForeColor = $gray
$lblOut = New-Ctl Label 20 436 720 20 'Архив сохранится в папку:'
$txtOut = New-Ctl TextBox 20 458 610 26 $cfg.OutDir
$btnBrowse = New-Ctl Button 640 456 100 28 'Обзор…'
$lblTemp = New-Ctl Label 20 490 720 20 ("Временные файлы: {0} — удаляются после упаковки" -f $tmp)
$lblTemp.ForeColor = $gray
$lblTemp.AutoEllipsis = $true
$pb = New-Ctl ProgressBar 20 522 720 18
$lblStatus = New-Ctl Label 20 546 720 40 'Нажмите «Начать сбор». Обычно это занимает от нескольких секунд до пары минут.'

# элементы итогового экрана
$lblNext = New-Ctl Label 20 380 720 82
$lblNext.Visible = $false
$lblUrl = New-Ctl Label 20 470 110 20 'Адрес LogAtlas:'
$txtUrl = New-Ctl TextBox 130 467 500 26 $cfg.Url
$btnOpen = New-Ctl Button 640 465 100 28 'Открыть'
foreach ($c in @($lblUrl, $txtUrl, $btnOpen)) { $c.Visible = $false }

$btnMain = New-Ctl Button 460 594 170 32 'Начать сбор'
$btnClose = New-Ctl Button 640 594 100 32 'Отмена'
$form.AcceptButton = $btnMain

$form.Controls.AddRange(@($lblTitle, $lblInfo, $lv, $lblSum, $lblPeriod, $cmbPeriod, $dtFrom, $lblDash, $dtTo, $lblTz, $lblOut, $txtOut, $btnBrowse, $lblTemp, $pb, $lblStatus,
                          $lblNext, $lblUrl, $txtUrl, $btnOpen, $btnMain, $btnClose))
if ($LaDesktop) { foreach ($c in @($lblOut, $txtOut, $btnBrowse)) { $c.Visible = $false } }

$avail = 0
foreach ($l in $logs) {
  $it = New-Object System.Windows.Forms.ListViewItem(('{0} — {1}' -f $l.Label, $l.Desc))
  $it.Tag = $l
  $it.ToolTipText = $l.Name
  if ($l.Exists) {
    $avail++
    [void]$it.SubItems.Add($(if ($null -ne $l.Records) { $l.Records.ToString('N0') } else { '?' }))
    [void]$it.SubItems.Add((Format-Size $l.Size))
    [void]$it.SubItems.Add('будет собран')
  } else {
    [void]$it.SubItems.Add('—')
    [void]$it.SubItems.Add('—')
    [void]$it.SubItems.Add('нет на этом компьютере')
    $it.ForeColor = [System.Drawing.Color]::Gray
  }
  [void]$lv.Items.Add($it)
}
foreach ($it in $lv.Items) { if ($it.Tag.Exists) { $it.Checked = $true } }

# Период: готовые варианты заполняют поля «с»/«по»; правка полей переключает на «Свой период»
$PRESET_HOURS = @(24, 72, 168, 720)
function Set-Preset {
  $i = $cmbPeriod.SelectedIndex
  $st.Filling = $true
  if ($i -ge 0 -and $i -le 3) {
    $now = Get-Date
    $dtTo.Value = $now
    $dtFrom.Value = $now.AddHours(-$PRESET_HOURS[$i])
  }
  $all = ($i -eq 4)
  $dtFrom.Enabled = -not $all; $dtTo.Enabled = -not $all; $lblDash.Enabled = -not $all
  $st.Filling = $false
  Update-Sum
}
$cmbPeriod.Add_SelectedIndexChanged({ Set-Preset })
foreach ($d in @($dtFrom, $dtTo)) {
  $d.Add_ValueChanged({ if (-not $st.Filling -and $cmbPeriod.SelectedIndex -ne 5) { $st.Filling = $true; $cmbPeriod.SelectedIndex = 5; $st.Filling = $false }; Update-Sum })
}
function Get-Period {
  # возвращает @{ From; To; Query; Text } по текущему выбору; From/To — местное время
  $i = $cmbPeriod.SelectedIndex
  if ($i -eq 4) { return @{ From = $null; To = $null; Query = ''; Text = 'всё время' } }
  if ($i -ge 0 -and $i -le 3) { $to = Get-Date; $from = $to.AddHours(-$PRESET_HOURS[$i]) }
  else { $from = $dtFrom.Value; $to = $dtTo.Value }
  $f = $from.ToUniversalTime().ToString("yyyy-MM-dd'T'HH:mm:ss.000'Z'")
  $t = $to.ToUniversalTime().ToString("yyyy-MM-dd'T'HH:mm:59.999'Z'")
  return @{
    From = $from; To = $to
    Query = "*[System[TimeCreated[@SystemTime>='$f' and @SystemTime<='$t']]]"
    Text = '{0:dd.MM.yyyy HH:mm} — {1:dd.MM.yyyy HH:mm} ({2})' -f $from, $to, $lblTz.Text
  }
}

function Update-Sum {
  if ($st.Phase -ne 'intro') { return }
  $sel = @($lv.Items | Where-Object { $_.Checked })
  $bytes = 0
  foreach ($i in $sel) { $bytes += $i.Tag.Size; $i.SubItems[3].Text = 'будет собран' }
  foreach ($i in $lv.Items) { if ($i.Tag.Exists -and -not $i.Checked) { $i.SubItems[3].Text = 'пропустить' } }
  $badRange = ($cmbPeriod.SelectedIndex -eq 5 -and $dtFrom.Value -ge $dtTo.Value)
  if ($badRange) {
    $lblSum.Text = 'Начало периода должно быть раньше конца.'
    $lblSum.ForeColor = [System.Drawing.Color]::Firebrick
  } else {
    $lblSum.ForeColor = [System.Drawing.SystemColors]::ControlText
    $whole = if ($cmbPeriod.SelectedIndex -eq 4) { 'объём до сжатия около {0}' -f (Format-Size $bytes) } else { 'журналы целиком — {0}, выгрузятся только события за период' -f (Format-Size $bytes) }
    $lblSum.Text = 'Выбрано журналов: {0} из {1} · {2}' -f $sel.Count, $avail, $whole
  }
  $btnMain.Enabled = ($sel.Count -gt 0 -and -not $badRange)
}

$lv.Add_ItemCheck({
  param($s, $e)
  $item = $lv.Items[$e.Index]
  if ($st.Phase -ne 'intro' -or -not $item.Tag.Exists) { $e.NewValue = $e.CurrentValue }
})
$lv.Add_ItemChecked({ Update-Sum })

$btnBrowse.Add_Click({
  if ($st.Phase -eq 'done') {
    [System.Windows.Forms.Clipboard]::SetText($st.Zip)
    $btnBrowse.Text = 'Скопировано'
    return
  }
  $dlg = New-Object System.Windows.Forms.FolderBrowserDialog
  $dlg.Description = 'Куда сохранить архив с журналами'
  if (Test-Path -LiteralPath $txtOut.Text) { $dlg.SelectedPath = $txtOut.Text }
  if ($dlg.ShowDialog($form) -eq 'OK') { $txtOut.Text = $dlg.SelectedPath }
})

function Set-Row([int]$k) {
  $item = $st.Items[$k]
  $text = $sync.Status[$k]
  if ($text -and $item.SubItems[3].Text -ne $text) {
    $item.SubItems[3].Text = $text
    switch ($sync.State[$k]) {
      'run' { $item.ForeColor = [System.Drawing.Color]::RoyalBlue }
      'ok'  { $item.ForeColor = [System.Drawing.Color]::DarkGreen }
      'err' { $item.ForeColor = [System.Drawing.Color]::Firebrick }
      'empty' { $item.ForeColor = [System.Drawing.Color]::Gray }
    }
    if ($sync.State[$k] -eq 'run') { $item.EnsureVisible() }
  }
}

function Start-Collect {
  $out = $txtOut.Text.Trim()
  try {
    if (-not $out) { throw 'папка не указана' }
    if (-not (Test-Path -LiteralPath $out)) { New-Item -ItemType Directory -Path $out -Force -ErrorAction Stop | Out-Null }
    $probe = Join-Path $out ('.logatlas-' + [guid]::NewGuid().ToString('N') + '.tmp')
    [IO.File]::WriteAllText($probe, 'x')
    Remove-Item -LiteralPath $probe -Force
  } catch {
    Show-Msg ("Не получается сохранить архив в папку:`n{0}`n`n{1}`n`nВыберите другую папку кнопкой «Обзор…»." -f $out, $_.Exception.Message) 'Warning'
    return
  }
  $per = Get-Period
  if ($per.From -and $per.From -ge $per.To) { Show-Msg 'Начало периода должно быть раньше конца.' 'Warning'; return }
  $st.Period = $per
  $cfg.OutDir = (Resolve-Path -LiteralPath $out).Path
  $cfg.Period = $cmbPeriod.SelectedIndex
  Save-Cfg
  $suffix = if ($per.From) { '{0:yyyyMMdd_HHmm}-{1:yyyyMMdd_HHmm}' -f $per.From, $per.To } else { 'all_' + $stamp }
  $st.Zip = Join-Path $cfg.OutDir ('{0}-logs-{1}.zip' -f $env:COMPUTERNAME, $suffix)
  if (Test-Path -LiteralPath $st.Zip) { $st.Zip = Join-Path $cfg.OutDir ('{0}-logs-{1}-{2}.zip' -f $env:COMPUTERNAME, $suffix, $stamp) }
  $st.Items = @($lv.Items | Where-Object { $_.Checked })
  try { New-Item -ItemType Directory -Force -Path $tmp -ErrorAction Stop | Out-Null } catch {
    Show-Msg ("Не удалось создать временную папку:`n{0}`n`n{1}" -f $tmp, $_.Exception.Message) 'Error'
    return
  }

  $st.Phase = 'run'
  foreach ($i in $lv.Items) { if (-not $i.Checked) { $i.ForeColor = [System.Drawing.Color]::Gray } }
  foreach ($i in $st.Items) { $i.SubItems[3].Text = 'в очереди' }
  $lblTitle.Text = 'Идёт сбор журналов…'
  $lblInfo.Text = "Период: {0}. Журналы выгружаются во временную папку, затем упаковываются в архив:`n{1}" -f $per.Text, $st.Zip
  foreach ($c in @($cmbPeriod, $dtFrom, $dtTo)) { $c.Enabled = $false }
  $lblSum.Text = 'Журналов к выгрузке: {0}' -f $st.Items.Count
  $txtOut.ReadOnly = $true
  $btnBrowse.Enabled = $false
  $btnMain.Enabled = $false
  $btnMain.Text = 'Идёт сбор…'
  $btnClose.Text = 'Остановить'
  $pb.Maximum = $st.Items.Count
  $pb.Value = 0

  $names = [string[]]@($st.Items | ForEach-Object { $_.Tag.Name })
  $ps = [powershell]::Create()
  [void]$ps.AddScript($worker).AddArgument($sync).AddArgument($names).AddArgument($tmp).AddArgument($st.Zip).AddArgument([string]$per.Query)
  $sync.Phase = 'starting'
  $st.Ps = $ps
  $st.Handle = $ps.BeginInvoke()
  $st.Sw.Start()
  $timer.Start()
}

function Stop-Collect {
  $sync.Cancel = $true
  $p = $sync.Proc
  if ($p) { try { $p.Kill() } catch { } }
  $btnClose.Enabled = $false
  $lblStatus.Text = 'Остановка…'
}

function Show-Final {
  $pb.Style = 'Blocks'
  $btnClose.Text = 'Закрыть'
  $btnClose.Enabled = $true
  $lblSum.Visible = $false
  $lblTemp.Visible = $false
  $pb.Visible = $false
  foreach ($c in @($lblPeriod, $cmbPeriod, $dtFrom, $lblDash, $dtTo, $lblTz)) { $c.Visible = $false }
  $lv.Height = 250
  $form.Activate()
}

function Finish-Done {
  $st.Phase = 'done'
  Show-Final
  $errs = @($sync.State.Values | Where-Object { $_ -eq 'err' }).Count
  $lblTitle.Text = 'Готово — архив создан'
  $extra = if ($errs) { " · не выгрузилось: $errs (см. статус в списке)" } else { '' }
  $empty = @($sync.State.Values | Where-Object { $_ -eq 'empty' }).Count
  if ($empty) { $extra += " · без событий за период: $empty" }
  $lblInfo.Text = "Период: {0}`nАрхив: {1} · журналов: {2}, событий: {3} · заняло {4}{5}" -f $st.Period.Text, (Format-Size $sync.ZipSize), $sync.Saved, ([long]$sync.Events).ToString('N0'), $st.Sw.Elapsed.ToString('m\:ss'), $extra
  $lblOut.Location = New-Object System.Drawing.Point(20, 370)
  $lblOut.Text = 'Файл архива:'
  $txtOut.Location = New-Object System.Drawing.Point(20, 392)
  $txtOut.Text = $st.Zip
  $btnBrowse.Location = New-Object System.Drawing.Point(640, 390)
  $btnBrowse.Text = 'Копировать'
  $btnBrowse.Enabled = $true
  $lblNext.Text = ("Что дальше:`n" +
    "1. Откройте LogAtlas в браузере — кнопка «Открыть» ниже (адрес запоминается).`n" +
    "2. На главной странице нажмите «+ Загрузить» и перетащите архив в окно (или «Выбрать файлы»).`n" +
    "3. Нажмите «Загрузить и разобрать». Распаковывать архив не нужно — LogAtlas сделает это сам.")
  $lblNext.Location = New-Object System.Drawing.Point(20, 430)
  $lblUrl.Location = New-Object System.Drawing.Point(20, 520)
  $txtUrl.Location = New-Object System.Drawing.Point(130, 517)
  $btnOpen.Location = New-Object System.Drawing.Point(640, 515)
  $lblNext.Visible = $true
  foreach ($c in @($lblUrl, $txtUrl, $btnOpen)) { $c.Visible = $true }
  $lblStatus.Location = New-Object System.Drawing.Point(20, 552)
  $lblStatus.ForeColor = $gray
  $lblStatus.Text = 'В архиве — журналы безопасности этого компьютера. Не пересылайте его посторонним и удалите после анализа.'
  $btnMain.Text = 'Показать в Проводнике'
  $btnMain.Enabled = $true
  $form.AcceptButton = $btnMain
  if ($LaDesktop) {
    $lblNext.Text = ("Что дальше:`n" +
      "Архив передан в LogAtlas — разбор начнётся сам, ход виден в окне браузера.`n" +
      "Это окно можно закрыть.")
    foreach ($c in @($lblOut, $txtOut, $btnBrowse, $btnMain)) { $c.Visible = $false }
    $form.AcceptButton = $btnClose
  }
  [System.Media.SystemSounds]::Asterisk.Play()
}

function Finish-Fail {
  $st.Phase = 'failed'
  Show-Final
  $lblTitle.Text = 'Не удалось собрать журналы'
  $kept = Test-Path -LiteralPath $tmp
  $lblInfo.Text = $sync.Error + $(if ($kept) { "`nВыгруженные файлы оставлены в папке: $tmp" } else { '' })
  $lblStatus.Text = 'Статус каждого журнала — в списке выше.'
  if ($kept) { $btnMain.Text = 'Открыть папку'; $btnMain.Enabled = $true } else { $btnMain.Visible = $false }
  [System.Media.SystemSounds]::Hand.Play()
}

function Finish-Cancel {
  $st.Phase = 'cancelled'
  Show-Final
  $lblTitle.Text = 'Сбор остановлен'
  $lblInfo.Text = 'Архив не создан, временные файлы удалены.'
  for ($k = 0; $k -lt $st.Items.Count; $k++) {
    if ($sync.State[$k] -ne 'ok' -and $sync.State[$k] -ne 'err') {
      $st.Items[$k].SubItems[3].Text = 'не собран'
      $st.Items[$k].ForeColor = [System.Drawing.Color]::Gray
    }
  }
  $lblStatus.Text = ''
  $btnMain.Visible = $false
}

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 250
$timer.Add_Tick({
  $n = $st.Items.Count
  for ($k = 0; $k -lt $n; $k++) { Set-Row $k }
  $pb.Value = [math]::Min([int]$sync.Done, $pb.Maximum)
  $el = $st.Sw.Elapsed.ToString('m\:ss')
  switch ($sync.Phase) {
    'export' {
      if ($sync.Cancel) { break }
      $i = [int]$sync.Index
      $lblStatus.Text = 'Выгружается {0} из {1}: {2} · прошло {3}' -f ($i + 1), $n, $st.Items[$i].Tag.Label, $el
    }
    'zip' {
      if ($pb.Style -ne 'Marquee') { $pb.Style = 'Marquee'; $pb.MarqueeAnimationSpeed = 30 }
      $lblStatus.Text = 'Упаковка в архив… · прошло {0}' -f $el
    }
    'done'      { $timer.Stop(); $st.Sw.Stop(); Finish-Done }
    'error'     { $timer.Stop(); $st.Sw.Stop(); Finish-Fail }
    'cancelled' { $timer.Stop(); $st.Sw.Stop(); Finish-Cancel }
  }
})

$btnMain.Add_Click({
  switch ($st.Phase) {
    'intro'  { Start-Collect }
    'done'   { Start-Process explorer.exe ('/select,"{0}"' -f $st.Zip) }
    'failed' { Start-Process explorer.exe ('"{0}"' -f $tmp) }
  }
})
$btnOpen.Add_Click({
  $u = $txtUrl.Text.Trim()
  if (-not $u) { Show-Msg 'Введите адрес LogAtlas, например http://192.168.10.10:8484'; return }
  if ($u -notmatch '^[a-zA-Z][a-zA-Z0-9+.-]*://') { $u = 'http://' + $u; $txtUrl.Text = $u }
  $cfg.Url = $u
  Save-Cfg
  # через explorer.exe — чтобы браузер открылся с правами пользователя, а не администратора
  Start-Process explorer.exe ('"{0}"' -f $u)
})
$btnClose.Add_Click({
  if ($st.Phase -eq 'run') { Stop-Collect } else { $form.Close() }
})
$form.Add_FormClosing({
  param($s, $e)
  if ($st.Phase -ne 'run') { return }
  $r = [System.Windows.Forms.MessageBox]::Show('Сбор ещё идёт. Остановить и закрыть окно?', $AppTitle, 'YesNo', 'Question')
  if ($r -ne 'Yes') { $e.Cancel = $true; return }
  Stop-Collect
  $deadline = (Get-Date).AddSeconds(5)
  while ($st.Handle -and -not $st.Handle.IsCompleted -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 100 }
})

$cmbPeriod.SelectedIndex = $cfg.Period
Set-Preset
$form.ResumeLayout($false)
[void]$form.ShowDialog()
$timer.Dispose()
if ($st.Ps) { $st.Ps.Dispose() }
$form.Dispose()

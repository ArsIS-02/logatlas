<#
Сборка LogAtlas для Windows (x64): папка dist\LogAtlas, установщик и переносной zip.

  powershell -ExecutionPolicy Bypass -File windows\build.ps1 [-Version 1.4.0] [-SkipInstaller]

Нужно: Python 3.12 x64 в PATH (версия встроенного Python берётся такая же), MSVC (Developer PowerShell
или ilammy/msvc-dev-cmd в GitHub Actions), Inno Setup 6.3+ (ISCC.exe), доступ в интернет
(python.org, PyPI, GitHub — встроенный Python, зависимости и WinSW).

Файл сохранён в UTF-8 с BOM: без BOM Windows PowerShell 5.1 читает его как ANSI и ломает кириллицу.
#>
param(
  [string]$Version = '',
  [switch]$SkipInstaller,
  [string]$WinSWVersion = 'v2.12.0'
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$repo = Split-Path $PSScriptRoot -Parent
Set-Location $repo

function Step($t) { Write-Host "==> $t" -ForegroundColor Cyan }
function Check($what) { if ($LASTEXITCODE -ne 0) { throw "$what завершилось с кодом $LASTEXITCODE" } }

if (-not $Version) {
  $Version = (Select-String -Path app\main.py -Pattern '^VERSION = "(.+)"').Matches[0].Groups[1].Value
}
$num = (($Version -split '[^0-9]+' | Where-Object { $_ -ne '' } | Select-Object -First 3) + @('0', '0', '0'))[0..2] -join ','
Step "LogAtlas $Version"

$distRoot = Join-Path $repo 'dist'
$dist = Join-Path $distRoot 'LogAtlas'
$build = Join-Path $repo 'build\windows'
foreach ($d in @($dist, $build)) { if (Test-Path $d) { Remove-Item -Recurse -Force $d } }
New-Item -ItemType Directory -Force -Path $dist, $build, (Join-Path $dist 'service') | Out-Null

# --- встроенный Python той же версии, что и python в PATH (совпадает формат .pyc и колёс)
$pyVer = (& python -c "import platform, struct; print(platform.python_version(), struct.calcsize('P') * 8)").Split(' ')
Check 'python'
if ($pyVer[1] -ne '64') { throw 'Нужен 64-битный Python' }
$pv = $pyVer[0]
$short = ($pv.Split('.')[0..1] -join '')
Step "встроенный Python $pv"
$embedZip = Join-Path $build "python-$pv-embed-amd64.zip"
Invoke-WebRequest -UseBasicParsing -Uri "https://www.python.org/ftp/python/$pv/python-$pv-embed-amd64.zip" -OutFile $embedZip
Expand-Archive -Path $embedZip -DestinationPath (Join-Path $dist 'python')
# пути поиска модулей: стандартная библиотека, site-packages сборки и корень LogAtlas (пакет app)
$pth = Join-Path $dist "python\python$short._pth"
[IO.File]::WriteAllText($pth, "python$short.zip`r`n.`r`nLib\site-packages`r`n..`r`n", [Text.Encoding]::ASCII)

# --- зависимости
Step 'зависимости'
$site = Join-Path $dist 'python\Lib\site-packages'
& python -m pip install --disable-pip-version-check --no-compile --only-binary=:all: --target $site -r windows\requirements.txt
if ($LASTEXITCODE -ne 0) {
  Write-Host 'evtx без готового колеса — беру python-evtx' -ForegroundColor Yellow
  Get-Content windows\requirements.txt | Where-Object { $_ -notmatch '^evtx' } | Set-Content (Join-Path $build 'req.txt')
  & python -m pip install --disable-pip-version-check --no-compile --target $site -r (Join-Path $build 'req.txt') python-evtx
  Check 'pip install'
}
Get-ChildItem $site -Directory -Filter 'bin' | Remove-Item -Recurse -Force

# --- файлы LogAtlas
Step 'файлы LogAtlas'
foreach ($d in 'app', 'static', 'scripts', 'demo') { Copy-Item -Recurse $d (Join-Path $dist $d) }
Get-ChildItem $dist -Recurse -Directory -Filter '__pycache__' | Remove-Item -Recurse -Force
Copy-Item windows\launcher.pyw, windows\logatlas.ico, LICENSE, THIRD_PARTY.txt $dist
Copy-Item windows\README-Windows.txt (Join-Path $dist 'README.txt')
Copy-Item windows\install-service.cmd, windows\uninstall-service.cmd $dist
Copy-Item windows\LogAtlasService.xml, windows\service.ps1 (Join-Path $dist 'service')

# --- WinSW: служба Windows
Step "WinSW $WinSWVersion"
$winsw = Join-Path $dist 'service\LogAtlasService.exe'
try {
  Invoke-WebRequest -UseBasicParsing -Uri "https://github.com/winsw/winsw/releases/download/$WinSWVersion/WinSW-x64.exe" -OutFile $winsw
} catch {
  Write-Host 'WinSW-x64.exe не скачался — беру WinSW.NET461.exe (нужен .NET Framework 4.6.1, он есть в Windows 10/11)' -ForegroundColor Yellow
  Invoke-WebRequest -UseBasicParsing -Uri "https://github.com/winsw/winsw/releases/download/$WinSWVersion/WinSW.NET461.exe" -OutFile $winsw
}

# --- LogAtlas.exe (значок, сведения о версии)
Step 'LogAtlas.exe'
Copy-Item windows\launcher.c, windows\launcher.rc, windows\logatlas.ico $build
Set-Content -Path (Join-Path $build 'version.rc') -Encoding ASCII -Value @(
  "#define VERSION_NUM $num,0", "#define VERSION_STR `"$Version`"", '#include "launcher.rc"')
Push-Location $build
try {
  & rc.exe /nologo /fo launcher.res version.rc; Check 'rc'
  & cl.exe /nologo /O1 /W3 /utf-8 launcher.c launcher.res /link /SUBSYSTEM:WINDOWS /OUT:LogAtlas.exe user32.lib shell32.lib; Check 'cl'
} finally { Pop-Location }
Copy-Item (Join-Path $build 'LogAtlas.exe') $dist

# --- байт-код заранее: быстрее первый запуск, в Program Files писать не нужно
Step 'байт-код'
& python -m compileall -q -j 0 (Join-Path $dist 'app') $site | Out-Null
Check 'compileall'

# --- проверка: встроенный Python видит все модули (из чужой папки, чтобы не подхватить app\ из репозитория)
Push-Location $env:TEMP
$env:LOGATLAS_DATA = Join-Path $build 'check-data'     # import app.main создаёт папку данных
try {
  & (Join-Path $dist 'python\python.exe') -c "import app.main, starlette, uvicorn, multipart, orjson, pystray, PIL, importlib.util as u; assert u.find_spec('evtx') or u.find_spec('Evtx'), 'evtx'; print('модули на месте:', app.main.VERSION)"
  Check 'проверка модулей'
} finally { Pop-Location; Remove-Item Env:LOGATLAS_DATA }

# --- переносной zip
Step 'переносной zip'
$pname = "LogAtlas-$Version-portable-x64"
$pdir = Join-Path $build $pname
Copy-Item -Recurse $dist $pdir
Set-Content -Path (Join-Path $pdir 'portable.txt') -Encoding UTF8 -Value 'Переносная версия: данные хранятся в папке data рядом с LogAtlas.exe. Удалите этот файл, чтобы хранить их в %LOCALAPPDATA%\LogAtlas.'
$zip = Join-Path $distRoot "$pname.zip"
if (Test-Path $zip) { Remove-Item $zip }
Compress-Archive -Path $pdir -DestinationPath $zip -CompressionLevel Optimal

# --- установщик
if (-not $SkipInstaller) {
  Step 'установщик'
  $iscc = (Get-Command ISCC.exe -ErrorAction SilentlyContinue).Source
  if (-not $iscc) {
    $iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
              "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
  }
  if (-not $iscc) { throw 'Не найден Inno Setup 6 (ISCC.exe): choco install innosetup' }
  & $iscc /Qp "/DAppVer=$Version" "/DSrc=$dist" windows\logatlas.iss; Check 'ISCC'
}

# --- контрольные суммы
Step 'SHA256SUMS'
$files = Get-ChildItem $distRoot -File | Where-Object { $_.Name -like 'LogAtlas-*' -and $_.Extension -in '.exe', '.zip' }
$files | ForEach-Object { '{0}  {1}' -f (Get-FileHash $_.FullName -Algorithm SHA256).Hash.ToLower(), $_.Name } |
  Set-Content -Encoding ASCII (Join-Path $distRoot 'SHA256SUMS-windows.txt')
Get-ChildItem $distRoot -File | Format-Table Name, @{ n = 'MB'; e = { '{0:N1}' -f ($_.Length / 1MB) } } -AutoSize

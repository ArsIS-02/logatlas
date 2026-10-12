<#
Сборка LogAtlas для Windows (x64 или ARM64): папка dist\LogAtlas-<арх>, установщик и переносной zip.

  powershell -ExecutionPolicy Bypass -File windows\build.ps1 [-Arch x64|arm64] [-Version 1.4.0] [-SkipInstaller]

Собирается на компьютере той же архитектуры (в GitHub Actions: windows-latest и windows-11-arm).
Нужно: Python 3.12 этой архитектуры в PATH (встроенный Python берётся той же версии), MSVC (Developer
PowerShell или ilammy/msvc-dev-cmd), Inno Setup 6.3+ (ISCC.exe) и интернет: python.org (встроенный Python),
PyPI (зависимости), nuget.org (заголовки WebView2), GitHub (WinSW).

Файл сохранён в UTF-8 с BOM: без BOM Windows PowerShell 5.1 читает его как ANSI и ломает кириллицу.
#>
param(
  [ValidateSet('', 'x64', 'arm64')]
  [string]$Arch = '',
  [string]$Version = '',
  [switch]$SkipInstaller,
  [string]$WinSWVersion = 'v2.12.0'
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
# вывод Python в UTF-8: в консоли Windows по умолчанию cp1252/cp866, и кириллица роняет print
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$repo = Split-Path $PSScriptRoot -Parent
Set-Location $repo

function Step($t) { Write-Host "==> $t" -ForegroundColor Cyan }
function Check($what) { if ($LASTEXITCODE -ne 0) { throw "$what завершилось с кодом $LASTEXITCODE" } }

if (-not $Version) {
  $Version = (Select-String -Path app\main.py -Pattern '^VERSION = "(.+)"').Matches[0].Groups[1].Value
}
$num = (($Version -split '[^0-9]+' | Where-Object { $_ -ne '' } | Select-Object -First 3) + @('0', '0', '0'))[0..2] -join ','

# --- архитектура: та, на которой идёт сборка (Python в PATH должен ей соответствовать)
$py = (& python -c "import platform, struct; print(platform.python_version(), platform.machine(), struct.calcsize('P') * 8)").Split(' ')
Check 'python'
$pv = $py[0]
$pyArch = switch ($py[1].ToUpper()) { 'AMD64' { 'x64' } 'ARM64' { 'arm64' } default { $py[1] } }
if ($py[2] -ne '64') { throw 'Нужен 64-битный Python' }
if (-not $Arch) { $Arch = $pyArch }
if ($pyArch -ne $Arch) { throw "Python в PATH — $pyArch, а сборка для ${Arch}: соберите на компьютере $Arch с Python $Arch" }
$embedArch = @{ x64 = 'amd64'; arm64 = 'arm64' }[$Arch]
Step "LogAtlas $Version для Windows $Arch"

$distRoot = Join-Path $repo 'dist'
$dist = Join-Path $distRoot "LogAtlas-$Arch"
$build = Join-Path $repo "build\windows-$Arch"
foreach ($d in @($dist, $build)) { if (Test-Path $d) { Remove-Item -Recurse -Force $d } }
New-Item -ItemType Directory -Force -Path $dist, $build, (Join-Path $dist 'service') | Out-Null

# --- встроенный Python той же версии (совпадает формат .pyc и колёс)
$short = ($pv.Split('.')[0..1] -join '')
Step "встроенный Python $pv ($embedArch)"
$embedZip = Join-Path $build "python-$pv-embed-$embedArch.zip"
Invoke-WebRequest -UseBasicParsing -Uri "https://www.python.org/ftp/python/$pv/python-$pv-embed-$embedArch.zip" -OutFile $embedZip
Expand-Archive -Path $embedZip -DestinationPath (Join-Path $dist 'python')
# пути поиска модулей: стандартная библиотека, site-packages сборки и корень LogAtlas (пакет app)
$pth = Join-Path $dist "python\python$short._pth"
[IO.File]::WriteAllText($pth, "python$short.zip`r`n.`r`nLib\site-packages`r`n..`r`n", [Text.Encoding]::ASCII)

# --- зависимости: обязательные; orjson и evtx — по возможности готовыми колёсами
Step 'зависимости'
$site = Join-Path $dist 'python\Lib\site-packages'
$req = Get-Content windows\requirements.txt | Where-Object { $_ -and $_ -notmatch '^\s*#' }
$core = $req | Where-Object { $_ -notmatch '^(orjson|evtx)' }
$pipArgs = @('-m', 'pip', 'install', '--disable-pip-version-check', '--no-compile', '--target', $site)
& python @pipArgs @core
Check 'pip install'
& python @pipArgs --only-binary=:all: ($req | Where-Object { $_ -match '^orjson' })
if ($LASTEXITCODE -ne 0) { Write-Host 'orjson: нет готового колеса — ответы API будут собираться модулем json (медленнее)' -ForegroundColor Yellow }
& python @pipArgs --only-binary=:all: evtx
if ($LASTEXITCODE -ne 0) {
  Write-Host 'evtx: нет готового колеса — беру python-evtx (чистый Python, медленнее)' -ForegroundColor Yellow
  & python @pipArgs python-evtx
  Check 'pip install python-evtx'
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

# --- WinSW: служба Windows. x64 — самодостаточная сборка; ARM64 — сборка для .NET Framework 4.6.1+
#     (AnyCPU, работает на ARM64 без эмуляции; .NET Framework 4.8.1 входит в Windows 11)
Step "WinSW $WinSWVersion"
$winsw = Join-Path $dist 'service\LogAtlasService.exe'
$assets = if ($Arch -eq 'arm64') { @('WinSW.NET461.exe') } else { @('WinSW-x64.exe', 'WinSW.NET461.exe') }
$got = $false
foreach ($a in $assets) {
  try {
    Invoke-WebRequest -UseBasicParsing -Uri "https://github.com/winsw/winsw/releases/download/$WinSWVersion/$a" -OutFile $winsw
    $got = $true; Write-Host "   $a"; break
  } catch { Write-Host "   $a не скачался: $($_.Exception.Message)" -ForegroundColor Yellow }
}
if (-not $got) { throw 'WinSW не скачался' }

# --- WebView2 SDK (заголовки и статический загрузчик) из NuGet
Step 'WebView2 SDK'
$idx = Invoke-RestMethod -Uri 'https://api.nuget.org/v3-flatcontainer/microsoft.web.webview2/index.json'
$wvVer = @($idx.versions | Where-Object { $_ -notmatch '-' })[-1]
$wvPkg = Join-Path $build "webview2.$wvVer.zip"
Invoke-WebRequest -UseBasicParsing -Uri "https://api.nuget.org/v3-flatcontainer/microsoft.web.webview2/$wvVer/microsoft.web.webview2.$wvVer.nupkg" -OutFile $wvPkg
$wv = Join-Path $build 'webview2'
Expand-Archive -Path $wvPkg -DestinationPath $wv
$wvInc = Join-Path $wv 'build\native\include'
$wvLib = Join-Path $wv "build\native\$Arch\WebView2LoaderStatic.lib"
if (-not (Test-Path $wvLib)) { throw "В пакете WebView2 $wvVer нет $wvLib" }
Write-Host "   Microsoft.Web.WebView2 $wvVer"

# --- LogAtlas.exe: окно на WebView2
Step 'LogAtlas.exe'
Copy-Item windows\LogAtlas.cpp, windows\LogAtlas.rc, windows\LogAtlas.manifest, windows\logatlas.ico $build
Set-Content -Path (Join-Path $build 'version.h') -Encoding ASCII -Value @(
  '#ifndef LOGATLAS_VERSION_H', '#define LOGATLAS_VERSION_H',
  "#define LOGATLAS_VERSION_NUM $num,0", "#define LOGATLAS_VERSION_STR `"$Version`"", "#define LOGATLAS_VERSION_W L`"$Version`"",
  '#endif')
$libs = @($wvLib, 'user32.lib', 'gdi32.lib', 'shell32.lib', 'ole32.lib', 'advapi32.lib', 'winhttp.lib', 'version.lib', 'shlwapi.lib')
Push-Location $build
try {
  & rc.exe /nologo /fo LogAtlas.res LogAtlas.rc; Check 'rc'
  $common = @('/nologo', '/O2', '/W3', '/EHsc', '/utf-8', '/std:c++17', '/DUNICODE', '/D_UNICODE', "/I$wvInc", 'LogAtlas.cpp', 'LogAtlas.res')
  $link = @('/link', '/SUBSYSTEM:WINDOWS', '/MANIFEST:NO', '/OUT:LogAtlas.exe') + $libs
  # статическая библиотека C++ (/MT): exe не зависит от msvcp140.dll; если загрузчик WebView2 с ней
  # несовместим — /MD и библиотеки Visual C++ рядом с exe
  & cl.exe /MT @common @link
  $crt = $false
  if ($LASTEXITCODE -ne 0) {
    Write-Host '/MT не собралось — собираю с /MD' -ForegroundColor Yellow
    & cl.exe /MD @common @link; Check 'cl'
    $crt = $true
  }
} finally { Pop-Location }
Copy-Item (Join-Path $build 'LogAtlas.exe') $dist
if ($crt) {
  $redist = Get-ChildItem (Join-Path $env:VCToolsRedistDir "$Arch") -Directory -Filter 'Microsoft.VC*.CRT' | Select-Object -First 1
  Copy-Item (Join-Path $redist.FullName '*.dll') $dist
}

# --- байт-код заранее: быстрее первый запуск, в Program Files писать не нужно
Step 'байт-код'
& python -m compileall -q -j 0 (Join-Path $dist 'app') $site | Out-Null
Check 'compileall'

# --- проверка: встроенный Python видит все модули (из чужой папки, чтобы не подхватить app\ из репозитория)
Push-Location $env:TEMP
$env:LOGATLAS_DATA = Join-Path $build 'check-data'     # import app.main создаёт папку данных
try {
  & (Join-Path $dist 'python\python.exe') -c "import app.main, starlette, uvicorn, multipart, pystray, PIL, importlib.util as u; assert u.find_spec('evtx') or u.find_spec('Evtx'), 'evtx'; print('modules ok, LogAtlas', app.main.VERSION, 'orjson' if u.find_spec('orjson') else 'json')"
  Check 'проверка модулей'
} finally { Pop-Location; Remove-Item Env:LOGATLAS_DATA }

# --- переносной zip
Step 'переносной zip'
$pname = "LogAtlas-$Version-portable-$Arch"
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
  & $iscc /Qp "/DAppVer=$Version" "/DSrc=$dist" "/DArch=$Arch" windows\logatlas.iss; Check 'ISCC'
}

# --- контрольные суммы
Step 'SHA256SUMS'
$files = Get-ChildItem $distRoot -File | Where-Object { $_.Name -like "LogAtlas-*-$Arch.*" -and $_.Extension -in '.exe', '.zip' }
$files | ForEach-Object { '{0}  {1}' -f (Get-FileHash $_.FullName -Algorithm SHA256).Hash.ToLower(), $_.Name } |
  Set-Content -Encoding ASCII (Join-Path $distRoot "SHA256SUMS-windows-$Arch.txt")
Get-ChildItem $distRoot -File | Format-Table Name, @{ n = 'MB'; e = { '{0:N1}' -f ($_.Length / 1MB) } } -AutoSize

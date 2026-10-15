; LogAtlas для Windows — установщик (Inno Setup 6.3+).
; Собирается windows\build.ps1: ISCC /DAppVer=1.4.0 /DArch=x64|arm64 /DSrc=<папка сборки> windows\logatlas.iss
; Папка сборки: LogAtlas.exe (окно), launcher.pyw, logatlas.ico, python\ (встроенный Python с зависимостями),
; app\, static\, scripts\, install-service.cmd, uninstall-service.cmd.
; Службу Windows ставит сам LogAtlas.exe (--install-service): сторонней обёртки служб и PowerShell в поставке нет.

#ifndef AppVer
  #define AppVer "2.0.0"
#endif
#ifndef Arch
  #define Arch "x64"
#endif
#ifndef Src
  #define Src "..\dist\LogAtlas-" + Arch
#endif

[Setup]
AppId={{4D31E17B-8243-45F7-8C9B-B9FCCBA85269}
AppName=LogAtlas
AppVersion={#AppVer}
AppVerName=LogAtlas {#AppVer}
AppPublisher=ArsIS-02
AppPublisherURL=https://github.com/ArsIS-02/logatlas
AppSupportURL=https://github.com/ArsIS-02/logatlas/issues
AppUpdatesURL=https://github.com/ArsIS-02/logatlas/releases
VersionInfoVersion={#AppVer}
DefaultDirName={autopf}\LogAtlas
DefaultGroupName=LogAtlas
DisableProgramGroupPage=yes
; «Для всех пользователей» (нужны права администратора, можно поставить службу) или «Только для меня»
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=dialog
#if Arch == "arm64"
ArchitecturesAllowed=arm64
ArchitecturesInstallIn64BitMode=arm64
#else
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
#endif
; Windows 10 1809 и новее: там есть встроенный клиент OpenSSH для сбора с хостов
MinVersion=10.0.17763
OutputDir=..\dist
OutputBaseFilename=LogAtlas-Setup-{#AppVer}-{#Arch}
SetupIconFile=logatlas.ico
UninstallDisplayIcon={app}\LogAtlas.exe
UninstallDisplayName=LogAtlas {#AppVer} ({#Arch})
WizardStyle=modern
WizardSmallImageFile=wizard-small.bmp,wizard-small-2x.bmp
LicenseFile=..\LICENSE
Compression=lzma2/max
SolidCompression=yes
CloseApplications=no
UsedUserAreasWarning=no
ShowLanguageDialog=no

[Languages]
Name: "ru"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "Значок на рабочем столе"
Name: "service"; Description: "Установить как службу Windows: панель работает без входа пользователя и открывается с других компьютеров локальной сети (порт 8484)"; Flags: unchecked; Check: IsAdminInstallMode

[Files]
Source: "{#Src}\*"; DestDir: "{app}"; Excludes: "portable.txt,data"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\LogAtlas"; Filename: "{app}\LogAtlas.exe"; WorkingDir: "{app}"; Comment: "Анализатор журналов macOS, Windows и Linux"
Name: "{autoprograms}\LogAtlas — сборщик журналов Windows"; Filename: "{app}\scripts\collect-windows-gui.cmd"; WorkingDir: "{app}\scripts"; IconFilename: "{sys}\eventvwr.exe"; Comment: "Выгрузить журналы событий этого компьютера в zip-архив"
Name: "{autodesktop}\LogAtlas"; Filename: "{app}\LogAtlas.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\LogAtlas.exe"; Parameters: "--install-service --quiet"; StatusMsg: "Установка и запуск службы LogAtlas…"; Flags: waituntilterminated; Tasks: service
Filename: "{app}\LogAtlas.exe"; Description: "Открыть LogAtlas"; Flags: postinstall nowait skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\app"
Type: filesandordirs; Name: "{app}\python"
Type: filesandordirs; Name: "{app}\static"
Type: filesandordirs; Name: "{app}\scripts"
Type: files; Name: "{app}\*.dll"
Type: dirifempty; Name: "{app}"

[Code]
var
  ServiceWasInstalled: Boolean;

function ServiceExists(): Boolean;
var
  Code: Integer;
begin
  Result := Exec(ExpandConstant('{sys}\sc.exe'), 'query LogAtlas', '', SW_HIDE, ewWaitUntilTerminated, Code) and (Code = 0);
end;

procedure RunService(Action: String);
var
  Code: Integer;
begin
  { Службу ставит и снимает сам LogAtlas.exe: сторонняя обёртка служб и запуск PowerShell с обходом
    политики выполнения из поставки убраны — на такие наборы признаков ругаются антивирусы. }
  Exec(ExpandConstant('{app}\LogAtlas.exe'), '--' + Action + '-service --quiet',
       ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, Code);
end;

{ Остановить службу перед заменой файлов. Через sc.exe, а не через LogAtlas.exe: при обновлении поверх
  прежней версии установлен именно старый LogAtlas.exe, который про эти ключи ещё не знает. }
procedure StopServiceForUpdate();
var
  Code, i: Integer;
begin
  if not ServiceExists() then exit;
  Exec(ExpandConstant('{sys}\sc.exe'), 'stop LogAtlas', '', SW_HIDE, ewWaitUntilTerminated, Code);
  for i := 1 to 40 do
  begin
    { sc только просит остановиться: ждём, пока состояние службы станет STOPPED (до 20 секунд) }
    if Exec(ExpandConstant('{sys}\cmd.exe'), '/c sc query LogAtlas | findstr /C:"STOPPED" >nul', '',
            SW_HIDE, ewWaitUntilTerminated, Code) and (Code = 0) then break;
    Sleep(500);
  end;
  Sleep(500);
end;

{ Закрыть запущенные приложения LogAtlas, чтобы файлы можно было заменить или удалить }
procedure QuitApps();
var
  Code: Integer;
begin
  if FileExists(ExpandConstant('{app}\launcher.pyw')) and FileExists(ExpandConstant('{app}\python\pythonw.exe')) then
  begin
    Exec(ExpandConstant('{app}\python\pythonw.exe'), '"' + ExpandConstant('{app}\launcher.pyw') + '" --quit',
         ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, Code);
    Sleep(2500);
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  ServiceWasInstalled := IsAdminInstallMode and ServiceExists();
  if ServiceWasInstalled and FileExists(ExpandConstant('{app}\LogAtlas.exe')) then
  begin
    StopServiceForUpdate();
    Sleep(1500);
  end;
  QuitApps();
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  { службу сняли с выбора при обновлении — удаляем её; если выбрана, её переустановит раздел [Run] }
  if (CurStep = ssPostInstall) and ServiceWasInstalled and not WizardIsTaskSelected('service') then
    RunService('uninstall');
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
  begin
    if ServiceExists() then
      RunService('uninstall');
    QuitApps();
  end;
  if (CurUninstallStep = usPostUninstall) and not UninstallSilent() then
    MsgBox('LogAtlas удалён. Собранные журналы остались в папке данных:' + #13#10 +
           ExpandConstant('{localappdata}') + '\LogAtlas' + #13#10 +
           '(у службы — ' + ExpandConstant('{commonappdata}') + '\LogAtlas). Удалите её, если журналы больше не нужны.',
           mbInformation, MB_OK);
end;

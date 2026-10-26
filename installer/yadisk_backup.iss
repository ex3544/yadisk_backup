; Inno Setup 6: установщик Yandex.Disk Backup.
;
; Сборка (из корня проекта, после PyInstaller):
;   python build_installer.py
; или вручную:
;   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\yadisk_backup.iss
;
; /DAppVersion=... и /DReportKey=... передаёт build_installer.py.
; ReportKey попадает в собранный установщик, но НЕ в репозиторий.

#ifndef AppVersion
  #define AppVersion "35.4.4"
#endif

#ifndef ReportKey
  #define ReportKey ""
#endif

#define AppName "Yandex.Disk Backup"
#define AppPublisher "ex3544 (vovapuh's son)"
#define AppExeName "YandexDiskBackup.exe"
#define AppIdValue "{{8E4A3F42-6C1D-4A7E-9F2B-3544A1B2C3D4}"

[Setup]
AppId={#AppIdValue}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
VersionInfoVersion={#AppVersion}.0
VersionInfoCompany={#AppPublisher}
VersionInfoDescription={#AppName} Setup
VersionInfoProductName={#AppName}
VersionInfoProductVersion={#AppVersion}
DefaultDirName={autopf}\YandexDiskBackup
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableWelcomePage=no
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
WizardStyle=modern
Compression=lzma2/ultra64
SolidCompression=yes
OutputDir=..\installer_output
OutputBaseFilename=YandexDiskBackup-{#AppVersion}-setup
SetupIconFile=..\assets\app.ico
UninstallDisplayIcon={app}\{#AppExeName}
UninstallDisplayName={#AppName} {#AppVersion}
LicenseFile=..\LICENSE
CloseApplications=yes
RestartApplications=no
SetupLogging=yes

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Создать ярлык на рабочем столе"; GroupDescription: "Дополнительно:"
Name: "startup"; Description: "Запускать при входе в Windows"; GroupDescription: "Дополнительно:"; Flags: unchecked

[InstallDelete]
; Чистое обновление: старые модули _internal не должны смешиваться с новыми.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\dist\YandexDiskBackup\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
; Официальный загрузчик Microsoft Edge WebView2 (Evergreen Bootstrapper).
; dontcopy: файл распаковывается только если WebView2 действительно нет.
Source: "vendor\MicrosoftEdgeWebView2Setup.exe"; DestDir: "{tmp}"; Flags: dontcopy

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Registry]
Root: HKLM; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "YandexDiskBackup"; ValueData: """{app}\{#AppExeName}"""; Flags: uninsdeletevalue; Tasks: startup

[Run]
Filename: "{tmp}\MicrosoftEdgeWebView2Setup.exe"; Parameters: "/silent /install"; StatusMsg: "Установка Microsoft Edge WebView2..."; Flags: runhidden waituntilterminated; Check: ShouldInstallWebView2; AfterInstall: CheckWebView2Result
Filename: "{app}\{#AppExeName}"; Description: "Запустить {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: files; Name: "{app}\.report_key.txt"

[Code]
var
  WebView2BootstrapperReady: Boolean;

function WebView2Installed(): Boolean;
var
  Version: String;
begin
  Result :=
    RegQueryStringValue(HKLM, 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Version) or
    RegQueryStringValue(HKLM, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Version) or
    RegQueryStringValue(HKCU, 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Version);
end;

function InitializeSetup(): Boolean;
begin
  Result := True;
  if not WebView2Installed() then
    SuppressibleMsgBox(
      'Компонент Microsoft Edge WebView2 не найден.' + #13#10 +
      'Установщик попробует поставить его автоматически (нужен интернет).' + #13#10 +
      'Если не получится, приложение откроется в браузере.',
      mbInformation, MB_OK, IDOK);
end;

function ShouldInstallWebView2(): Boolean;
begin
  Result := WebView2BootstrapperReady and (not WebView2Installed());
end;

procedure CheckWebView2Result();
var
  I: Integer;
begin
  // Edge Bootstrapper иногда завершается раньше фактической установки.
  for I := 1 to 20 do
  begin
    if WebView2Installed() then
      Exit;
    Sleep(2000);
  end;
  SuppressibleMsgBox(
    'Не удалось установить WebView2 автоматически.' + #13#10 +
    'Приложение откроется в браузере. Для встроенного окна установите' + #13#10 +
    'Microsoft Edge WebView2 Runtime вручную.',
    mbInformation, MB_OK, IDOK);
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  KeyPath: String;
begin
  if CurStep = ssPostInstall then
  begin
    WebView2BootstrapperReady := False;
    if not WebView2Installed() then
    begin
      ExtractTemporaryFile('MicrosoftEdgeWebView2Setup.exe');
      WebView2BootstrapperReady :=
        FileExists(ExpandConstant('{tmp}\MicrosoftEdgeWebView2Setup.exe'));
      if not WebView2BootstrapperReady then
        SuppressibleMsgBox(
          'Не удалось распаковать установщик WebView2.' + #13#10 +
          'Приложение всё равно установится и откроется в браузере.',
          mbInformation, MB_OK, IDOK);
    end;

    if '{#ReportKey}' <> '' then
    begin
      KeyPath := ExpandConstant('{app}\.report_key.txt');
      if not SaveStringToFile(KeyPath, '{#ReportKey}', False) then
        MsgBox(
          'Не удалось сохранить ключ отправки отчётов.' + #13#10 +
          'Его можно ввести вручную на вкладке «Диагностика».',
          mbError, MB_OK);
    end;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    SuppressibleMsgBox(
      'Базы, настройки и куки сохранены в папке:' + #13#10 +
      ExpandConstant('{localappdata}\YandexDiskBackup') + #13#10 + #13#10 +
      'Удалите её вручную, если данные больше не нужны.',
      mbInformation, MB_OK, IDOK);
end;

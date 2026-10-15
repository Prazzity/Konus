; Inno Setup script for the Konus installer.
;
;   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" /DAppVersion=0.4.2 packaging\konus.iss
;
; `tools/build_installer.py` builds the application with PyInstaller first and passes the
; version, so nobody has to type it here. What it makes is a per-user install by default
; (no administrator asked for, nothing outside the user's own folders): the wizard can still
; be told to install for everybody, and then it asks for the elevation itself.
;
; The build is GPLv3 as a whole — PyQt5 and mutagen are GPL, libVLC and python-vlc are
; LGPL-2.1-or-later, the project's own code is MIT — so the licence page shows the GPLv3, and
; LICENSE and licenses\ travel in the installed folder.

#define AppName "Konus"
#define AppExe "Konus.exe"
#define AppPublisher "Prazzity"
#define AppUrl "https://github.com/Prazzity/Konus"

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
; The AppId is what makes this trade an older install for a newer one instead of piling up:
; it must stay the same for every release of Konus.
AppId={{AE8E6138-DE6B-422D-88FF-7B590B972E0C}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppUrl}
AppSupportURL={#AppUrl}/issues
AppUpdatesURL={#AppUrl}/releases
VersionInfoVersion={#AppVersion}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
AllowNoIcons=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
LicenseFile=..\licenses\GPL-3.0.txt
SetupIconFile=..\konus\assets\konus.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName} {#AppVersion}
OutputDir=..\dist\release
OutputBaseFilename={#AppName}-{#AppVersion}-setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[CustomMessages]
english.DeleteData=Delete your settings, library, playlists and cache as well?%n%nThey live in the konus folders under AppData. Answer No to keep them for a later install.
russian.DeleteData=Удалить также настройки, библиотеку, плейлисты и кэш?%n%nОни лежат в папках konus внутри AppData. Ответьте «Нет», чтобы сохранить их для будущей установки.

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\Konus\{#AppExe}"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist\Konus\_internal\*"; DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\dist\Konus\vendor\*"; DestDir: "{app}\vendor"; Flags: ignoreversion recursesubdirs
Source: "..\dist\Konus\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist\Konus\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist\Konus\README.ru.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist\Konus\licenses\*"; DestDir: "{app}\licenses"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#StringChange(AppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ConfigDir, CacheDir: String;
begin
  if CurUninstallStep <> usPostUninstall then
    exit;
  ConfigDir := ExpandConstant('{userappdata}\konus');
  CacheDir := ExpandConstant('{localappdata}\konus');
  if (not DirExists(ConfigDir)) and (not DirExists(CacheDir)) then
    exit;
  if UninstallSilent then
    exit;
  if MsgBox(ExpandConstant('{cm:DeleteData}'), mbConfirmation, MB_YESNO) = IDYES then
  begin
    DelTree(ConfigDir, True, True, True);
    DelTree(CacheDir, True, True, True);
  end;
end;

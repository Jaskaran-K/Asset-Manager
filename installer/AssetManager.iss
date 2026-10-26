; Inno Setup 6 script for the Asset Manager installer (dist\AssetManager-Setup.exe).
; Packages the PyInstaller folder build (dist\AssetManager\), which starts much faster than the
; portable single-file exe because nothing has to be unpacked at launch.
;
; Build:  iscc /DAppVersion=0.3.0 installer\AssetManager.iss      (after: pyinstaller AssetManager.spec)
;
; Installs per user (no administrator prompt), so the in-app updater can run it silently:
;   AssetManager-Setup.exe /SILENT /SUPPRESSMSGBOXES /NORESTART
; which closes the running app, updates it in place and starts it again.
; /NOLAUNCH skips starting the app afterwards (used by the release workflow's install test).
; Settings (~\.3d_asset_manager_config.json) and the library (%LOCALAPPDATA%\AssetManager) are
; never touched by installing, updating or uninstalling.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{6F1B6E52-6C1D-4E57-9D86-3C8A8B0B9E31}
AppName=Asset Manager
AppVersion={#AppVersion}
AppPublisher=Asset Manager
AppPublisherURL=https://github.com/Jaskaran-K/Asset-Manager
AppSupportURL=https://github.com/Jaskaran-K/Asset-Manager/issues
AppUpdatesURL=https://github.com/Jaskaran-K/Asset-Manager/releases
DefaultDirName={localappdata}\Programs\Asset Manager
DefaultGroupName=Asset Manager
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=AssetManager-Setup
SetupIconFile=..\assets\Asset-Manager-Icon.ico
UninstallDisplayIcon={app}\AssetManager.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=force
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[InstallDelete]
; Files from the previous version that the new one no longer ships
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\dist\AssetManager\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Asset Manager"; Filename: "{app}\AssetManager.exe"
Name: "{group}\Uninstall Asset Manager"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Asset Manager"; Filename: "{app}\AssetManager.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\AssetManager.exe"; Description: "{cm:LaunchProgram,Asset Manager}"; Flags: nowait postinstall; Check: ShouldLaunch

[Code]
function ShouldLaunch: Boolean;
var
  I: Integer;
begin
  Result := True;
  for I := 1 to ParamCount do
    if CompareText(ParamStr(I), '/NOLAUNCH') = 0 then
      Result := False;
end;

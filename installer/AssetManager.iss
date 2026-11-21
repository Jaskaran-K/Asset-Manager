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
;
; KeyDial (the Huion KD100's tray app, KeyDial.spec -> dist\KeyDial\) comes with it when the "keydial" box is
; ticked (it is to start with): {app}\KeyDial\KeyDial.exe. A silent update starts it again in the tray;
; its profiles (%APPDATA%\KeyDial) are never touched either.

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
Name: "keydial"; Description: "Also install KeyDial, for the Huion KD100's keys and dial (a tray app that starts with Windows)"; GroupDescription: "Bonus:"

[InstallDelete]
; Files from the previous version that the new one no longer ships
Type: filesandordirs; Name: "{app}\_internal"
Type: filesandordirs; Name: "{app}\KeyDial\_internal"

[Files]
Source: "..\dist\AssetManager\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\dist\KeyDial\*"; DestDir: "{app}\KeyDial"; Flags: ignoreversion recursesubdirs createallsubdirs; Tasks: keydial

[Icons]
Name: "{group}\Asset Manager"; Filename: "{app}\AssetManager.exe"
Name: "{group}\Asset Manager Theme Studio"; Filename: "{app}\AssetManager.exe"; Parameters: "--theme-studio"; Comment: "Make your own themes and backgrounds"
Name: "{group}\Uninstall Asset Manager"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Asset Manager"; Filename: "{app}\AssetManager.exe"; Tasks: desktopicon
Name: "{group}\KeyDial"; Filename: "{app}\KeyDial\KeyDial.exe"; Comment: "The Huion KD100's keys and dial"; Tasks: keydial

[Run]
Filename: "{app}\AssetManager.exe"; Description: "{cm:LaunchProgram,Asset Manager}"; Flags: nowait postinstall; Check: ShouldLaunch
Filename: "{app}\KeyDial\KeyDial.exe"; Description: "Open KeyDial"; Flags: nowait postinstall skipifsilent; Tasks: keydial; Check: ShouldLaunch
Filename: "{app}\KeyDial\KeyDial.exe"; Parameters: "--tray"; Flags: nowait; Tasks: keydial; Check: SilentUpdate

[UninstallRun]
; KeyDial: stop it, and take it out of Windows' start-up (its profiles in %APPDATA%\KeyDial stay)
Filename: "{sys}\taskkill.exe"; Parameters: "/IM KeyDial.exe /F"; Flags: runhidden; RunOnceId: "StopKeyDial"; Tasks: keydial
Filename: "{app}\KeyDial\KeyDial.exe"; Parameters: "--remove-autostart"; Flags: runhidden waituntilterminated; RunOnceId: "KeyDialStartup"; Tasks: keydial

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

function SilentUpdate: Boolean;
begin
  { The app's updater (silent): KeyDial starts again, in the tray, so the KD100 keeps working }
  Result := WizardSilent and ShouldLaunch;
end;

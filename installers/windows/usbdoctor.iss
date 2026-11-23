; Inno Setup script for USB Doctor AI.
; Build with:  ISCC.exe installers\windows\usbdoctor.iss
; The application folder must already exist (see build.ps1).

#define AppName        "USB Doctor AI"
#define AppVersion     "1.0.0"
#define AppPublisher   "USB Doctor AI"
#define AppURL         "https://github.com/Analoggeek/DriveRepair"
#define AppExeName     "USBDoctorAI.exe"
#define SourceDir      "..\..\dist\USBDoctorAI"

[Setup]
AppId={{8F3B2C61-71A4-4C0E-9E7B-USBDOCTORAI01}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/issues
AppUpdatesURL={#AppURL}/releases
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
LicenseFile=..\..\LICENSE
OutputDir=..\..\dist
OutputBaseFilename=USBDoctorAI-Setup-{#AppVersion}
SetupIconFile=..\..\usbdoctor\frontend\web\assets\app.ico
UninstallDisplayIcon={app}\{#AppExeName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; The app talks to raw devices, so it must run elevated to be useful.
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
DisableWelcomePage=no
ChangesAssociations=no
; Long paths are needed for deeply nested recovered folders.
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "quicklaunchicon"; Description: "Create a Quick Launch shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked

[Files]
Source: "{#SourceDir}\{#AppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; Documentation ships with the app so users have it offline.
Source: "..\..\docs\*"; DestDir: "{app}\docs"; Flags: ignoreversion recursesubdirs createallsubdirs skipifsourcedoesntexist
Source: "..\..\README.md"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "..\..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon
Name: "{userappdata}\Microsoft\Internet Explorer\Quick Launch\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: quicklaunchicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent runascurrentuser
; Optional smartmontools install (official, verified via hash before use).
Filename: "https://www.smartmontools.org/"; Description: "Open the official smartmontools page (optional, for SMART data)"; Flags: shellexec postinstall unchecked

[UninstallDelete]
Type: filesandordirs; Name: "{app}\docs"

[Code]
{ Refuse to run on 32-bit Windows: raw 64-bit device addressing is required. }
function InitializeSetup(): Boolean;
begin
  Result := True;
  if not IsWin64 then
  begin
    MsgBox('USB Doctor AI requires 64-bit Windows 10 or later.', mbError, MB_OK);
    Result := False;
  end;
end;

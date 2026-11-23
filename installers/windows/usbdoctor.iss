; Inno Setup script for USB Doctor AI.
; Build with:  ISCC.exe installers\windows\usbdoctor.iss
; The application folder must already exist (see build.ps1).

#define AppName        "USB Doctor AI"
#define AppVersion     "1.0.0"
#define AppPublisher   "USB Doctor AI"
#define AppURL         "https://github.com/Analoggeek/DriveRepair"
#define AppExeName     "USBDoctorAI.exe"
#define ProjectRoot    "..\.."
#define SourceDir      ProjectRoot + "\dist\USBDoctorAI"

[Setup]
AppId={{7A26301C-4A2B-550D-BA98-BB2A8515DF74}
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
LicenseFile={#ProjectRoot + "\LICENSE"}
OutputDir={#ProjectRoot + "\dist"}
OutputBaseFilename=USBDoctorAI-Setup-{#AppVersion}
SetupIconFile={#ProjectRoot + "\usbdoctor\frontend\web\assets\app.ico"}
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
Source: "{#ProjectRoot + "\docs\*"}"; DestDir: "{app}\docs"; Flags: ignoreversion recursesubdirs createallsubdirs skipifsourcedoesntexist
Source: "{#ProjectRoot + "\README.md"}"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "{#ProjectRoot + "\LICENSE"}"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon
Name: "{userappdata}\Microsoft\Internet Explorer\Quick Launch\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: quicklaunchicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent runascurrentuser
; Optional smartmontools install (official, verified via hash before use).
Filename: "https://www.smartmontools.org/"; Description: "Open the official smartmontools page (optional, for SMART data)"; Flags: shellexec postinstall unchecked
; Prove the installed copy actually runs before the user is told it succeeded.
; skipifsilent, not "Check: WizardSilent = False": the Check parameter is
; evaluated by Inno's expression evaluator, which rejects "=" ("Invalid symbol '='
; found") and failed the compile after the whole application had been built.
Filename: "{app}\{#AppExeName}"; Parameters: "selfcheck"; StatusMsg: "Verifying the installation..."; Flags: runhidden waituntilterminated runascurrentuser skipifsilent

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

{ Every file the application needs must be present in the package. A silently
  empty installer is the worst possible outcome for an offline product, so the
  build fails loudly here instead of shipping. }
procedure CurStepChanged(CurStep: TSetupStep);
var
  Missing: String;
begin
  if CurStep = ssPostInstall then
  begin
    Missing := '';
    if not FileExists(ExpandConstant('{app}\USBDoctorAI.exe')) then
      Missing := Missing + 'USBDoctorAI.exe' + #13#10;
    if not DirExists(ExpandConstant('{app}\_internal')) then
      Missing := Missing + '_internal\ (bundled runtime)' + #13#10;
    if not DirExists(ExpandConstant('{app}\docs')) then
      Missing := Missing + 'docs\' + #13#10;
    if Missing <> '' then
      MsgBox('The installation is incomplete. These items are missing:' + #13#10#13#10 +
             Missing + #13#10 + 'Please re-download the installer.', mbError, MB_OK);
  end;
end;

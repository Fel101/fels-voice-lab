; Setup.exe for Fel's Voice Lab. Build it with tools\build_installer.py: it stages the release's files, the Lab's own Python
; and the wheels of the core and the desktop window, and passes AppVersion, Repository, Staging, Output and ExtraBytes here.
;
; It installs for the current user, with no administrator prompt: the Lab writes into its own folder, so it cannot live in
; Program Files. After copying, it sets up the core (.venv) and the desktop window (.venv-desktop) from the wheels it
; carries, so the window opens with no internet; the speech models are installed afterwards from the Models page.
; Uninstalling asks before it touches the library and keeps it unless told otherwise (tools\uninstall_lab.py).

#ifndef AppVersion
  #error Build Setup.exe with tools\build_installer.py.
#endif
#if Ver < EncodeVer(6, 7, 0)
  #error Inno Setup 6.7 or later is needed.
#endif

#define AppName "Fel's Voice Lab"
#define WindowPython "{app}\.venv-desktop\Scripts\pythonw.exe"
#define SetupLog "{app}\out\install\setup-exe.log"

[Setup]
AppId={{6F1D2A8B-5C3E-4B7A-9D21-3E8F0C4A7B15}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Fel's Voice Lab project
AppPublisherURL=https://github.com/{#Repository}
AppSupportURL=https://github.com/{#Repository}/issues
AppUpdatesURL=https://github.com/{#Repository}/releases
; Short, with no spaces or apostrophe, and not synced by OneDrive.
DefaultDirName={localappdata}\FelsVoiceLab
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#Output}
OutputBaseFilename=FelsVoiceLab-Setup-{#AppVersion}
SetupIconFile={#Staging}\app\assets\voice-lab.ico
UninstallDisplayIcon={app}\assets\voice-lab.ico
UninstallDisplayName={#AppName}
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
; What setup puts in .venv and .venv-desktop, on top of the files copied.
ExtraDiskSpaceRequired={#ExtraBytes}
SetupLogging=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Messages]
DiskSpaceMBLabel=The Lab and its Python packages need at least [mb] MB. The speech models are installed later, from the Models page, and need more: about 2 GB for Kokoro and Phonon, and several GB for each larger model.

[Tasks]
Name: "desktopicon"; Description: "Put a shortcut on the &desktop"; Flags: unchecked

[Files]
Source: "{#Staging}\app\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#Staging}\python\*"; DestDir: "{app}\python"; Flags: ignoreversion recursesubdirs createallsubdirs
; Only for setting up the core and the window; {tmp} is deleted when Setup ends.
Source: "{#Staging}\wheels\*"; DestDir: "{tmp}\wheels"; Flags: ignoreversion

[Icons]
Name: "{userprograms}\{#AppName}"; Filename: "{#WindowPython}"; Parameters: """{app}\desktop.py"""; WorkingDir: "{app}"; IconFilename: "{app}\assets\voice-lab.ico"; Comment: "Local voice design, casting and dialogue studio"
Name: "{userdesktop}\{#AppName}"; Filename: "{#WindowPython}"; Parameters: """{app}\desktop.py"""; WorkingDir: "{app}"; IconFilename: "{app}\assets\voice-lab.ico"; Comment: "Local voice design, casting and dialogue studio"; Tasks: desktopicon

[Run]
Filename: "{#WindowPython}"; Parameters: """{app}\desktop.py"""; WorkingDir: "{app}"; Description: "Open {#AppName} now"; Flags: postinstall nowait skipifsilent; Check: SetUpWorked

[Code]
var
  SetUpOk: Boolean;

function SetUpWorked: Boolean;
begin
  Result := SetUpOk;
end;

{ The OneDrive folder that holds Folder, or ''. The same rule as tools\setup_core.py's onedrive_folder, with its variables. }
function OneDriveFolder(Folder: String): String;
var
  Names: array of String;
  I: Integer;
  Base: String;
begin
  Result := '';
  SetArrayLength(Names, 3);
  Names[0] := 'OneDrive';
  Names[1] := 'OneDriveConsumer';
  Names[2] := 'OneDriveCommercial';
  Folder := AddBackslash(Lowercase(ExpandFileName(Folder)));
  for I := 0 to GetArrayLength(Names) - 1 do
  begin
    Base := GetEnv(Names[I]);
    if Base <> '' then
      if Copy(Folder, 1, Length(AddBackslash(Lowercase(ExpandFileName(Base))))) = AddBackslash(Lowercase(ExpandFileName(Base))) then
      begin
        Result := Base;
        Exit;
      end;
  end;
end;

procedure InitializeWizard;
begin
  { Room for the line about the models under the folder box. }
  WizardForm.DiskSpaceLabel.WordWrap := True;
  WizardForm.DiskSpaceLabel.Top := WizardForm.DiskSpaceLabel.Top - ScaleY(26);
  WizardForm.DiskSpaceLabel.Height := WizardForm.DiskSpaceLabel.Height + ScaleY(26);
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  Sync: String;
begin
  Result := True;
  if CurPageID = wpSelectDir then
  begin
    Sync := OneDriveFolder(WizardDirValue);
    if Sync <> '' then
      Result := SuppressibleMsgBox('This folder is inside OneDrive (' + Sync + '). OneDrive would upload the gigabytes of ' +
        'packages and models the Lab installs, and can lock its databases while it syncs them. Choose a folder OneDrive ' +
        'does not sync, such as the one Setup offers.' + #13#10#13#10 + 'Install here anyway?',
        mbConfirmation, MB_YESNO or MB_DEFBUTTON2, IDNO) = IDYES;
  end;
end;

{ Run one setup step with its output added to the log, showing what it does. True when it ended with exit code 0. }
function SetUpStep(Status, Exe, Arguments: String): Boolean;
var
  Code: Integer;
begin
  WizardForm.StatusLabel.Caption := Status;
  WizardForm.FilenameLabel.Caption := 'Its progress goes to ' + ExpandConstant('{#SetupLog}');
  Log(Status);
  Result := Exec(ExpandConstant('{cmd}'), '/S /C ""' + Exe + '" ' + Arguments + ' >> "' +
    ExpandConstant('{#SetupLog}') + '" 2>&1"', ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, Code) and (Code = 0);
  if not Result then
    Log(Format('The step ended with exit code %d.', [Code]));
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Failed: String;
begin
  if CurStep <> ssPostInstall then
    Exit;
  ForceDirectories(ExpandConstant('{app}\out\install'));
  WizardForm.ProgressGauge.Style := npbstMarquee;
  Failed := '';
  if not SetUpStep('Setting up the core: the Python packages Setup carries, so no internet is needed ...',
      ExpandConstant('{app}\python\python.exe'),
      '"' + ExpandConstant('{app}\tools\setup_core.py') + '" --wheels "' + ExpandConstant('{tmp}\wheels') + '"') then
    Failed := 'the core'
  else if not SetUpStep('Setting up the desktop window ...',
      ExpandConstant('{app}\.venv\Scripts\python.exe'),
      '"' + ExpandConstant('{app}\tools\setup_desktop.py') + '" --wheels "' + ExpandConstant('{tmp}\wheels') + '" --no-shortcut') then
    Failed := 'the desktop window';
  WizardForm.ProgressGauge.Style := npbstNormal;
  SetUpOk := Failed = '';
  if not SetUpOk then
    SuppressibleMsgBox('Setup copied the Lab but could not set up ' + Failed + '. What went wrong is at the end of ' +
      ExpandConstant('{#SetupLog}') + '.' + #13#10#13#10 + 'Run this installer again, or run setup.bat in ' +
      ExpandConstant('{app}') + ', to finish.', mbError, MB_OK, IDOK);
end;

{ Setup's exit code is 1 when the core or the window was not set up, so a silent install that failed says so. }
function GetCustomSetupExitCode: Integer;
begin
  if SetUpOk then
    Result := 0
  else
    Result := 1;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Code: Integer;
  Choice: String;
begin
  if CurUninstallStep = usUninstall then
  begin
    if SuppressibleMsgBox('Keep your library?' + #13#10#13#10 + 'Yes keeps your voices, takes, scenes, comparisons, ' +
        'exports and backups in ' + ExpandConstant('{app}') + ', where the Lab finds them if you install it there again. ' +
        'No deletes them too.' + #13#10#13#10 + 'The program, its Python environments and the speech models are removed ' +
        'either way. Close the Lab before you go on.', mbConfirmation, MB_YESNO, IDYES) = IDYES then
      Choice := '--keep-library'
    else
      Choice := '--everything';
    if FileExists(ExpandConstant('{app}\python\python.exe')) and FileExists(ExpandConstant('{app}\tools\uninstall_lab.py')) then
      Exec(ExpandConstant('{app}\python\python.exe'), '"' + ExpandConstant('{app}\tools\uninstall_lab.py') + '" ' + Choice,
        ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, Code);
  end;
  if CurUninstallStep = usPostUninstall then
  begin
    { The Python the step above ran on, then the folder itself when nothing was kept in it. }
    DelTree(ExpandConstant('{app}\python'), True, True, True);
    RemoveDir(ExpandConstant('{app}'));
  end;
end;

; Setup.exe for Fel's Voice Lab. Build it with tools\build_installer.py: it stages the release's files, the Lab's own Python
; and the wheels of the core and the desktop window, and passes AppVersion, Repository, Staging, Output and ExtraBytes here.
;
; It installs for the current user, with no administrator prompt: the Lab writes into its own folder, so it cannot live in
; Program Files. After copying, it sets up the core (.venv) and the desktop window (.venv-desktop) from the wheels it
; carries, so the window opens with no internet; the speech models are installed afterwards from the Models page.
;
; Over an installed Lab it is an update, as update.bat's: it says "update from X to Y", refuses an older version over a newer
; one and waits for the Lab to close (the installed Lab's own update.py checks, with the Lab's own Python), and after copying
; it runs the new update.py --after-setup, which records the update and finishes it as update.bat does. Before copying,
; [InstallDelete] backs up and removes pristine files the new version dropped. Its last page says what the update found.
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
; Only for setting up the core and the window, and for an update; {tmp} is deleted when Setup ends.
Source: "{#Staging}\wheels\*"; DestDir: "{tmp}\wheels"; Flags: ignoreversion
Source: "{#Staging}\releases.json"; DestDir: "{tmp}"; Flags: ignoreversion

[InstallDelete]
#include Staging + "\retired.iss"

[Icons]
Name: "{userprograms}\{#AppName}"; Filename: "{#WindowPython}"; Parameters: """{app}\desktop.py"""; WorkingDir: "{app}"; IconFilename: "{app}\assets\voice-lab.ico"; Comment: "Local voice design, casting and dialogue studio"
Name: "{userdesktop}\{#AppName}"; Filename: "{#WindowPython}"; Parameters: """{app}\desktop.py"""; WorkingDir: "{app}"; IconFilename: "{app}\assets\voice-lab.ico"; Comment: "Local voice design, casting and dialogue studio"; Tasks: desktopicon

[Run]
Filename: "{#WindowPython}"; Parameters: """{app}\desktop.py"""; WorkingDir: "{app}"; Description: "Open {#AppName} now"; Flags: postinstall nowait skipifsilent; Check: SetUpWorked

[Code]
var
  SetUpOk: Boolean;
  { The version of the Lab in the chosen folder before Setup copies anything, or '' when there is none. }
  InstalledVersion: String;

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

{ VERSION in a Lab's app_version.py, or '' when the folder holds no Lab. }
function LabVersionIn(Folder: String): String;
var
  Lines: TArrayOfString;
  I: Integer;
  Line: String;
begin
  Result := '';
  if not LoadStringsFromFile(AddBackslash(Folder) + 'app_version.py', Lines) then
    Exit;
  for I := 0 to GetArrayLength(Lines) - 1 do
  begin
    Line := Trim(Lines[I]);
    if (Copy(Line, 1, 11) = 'VERSION = "') and (Copy(Line, Length(Line), 1) = '"') then
    begin
      Result := Copy(Line, 12, Length(Line) - 12);
      Exit;
    end;
  end;
end;

{ The first number of a version such as 0.3.0, taken off its front. }
function TakeNumber(var Version: String): Integer;
var
  Dot: Integer;
begin
  Dot := Pos('.', Version);
  if Dot = 0 then
    Dot := Length(Version) + 1;
  Result := StrToIntDef(Copy(Version, 1, Dot - 1), 0);
  Version := Copy(Version, Dot + 1, Length(Version));
end;

{ Below 0 when version A is older than B, 0 when they are the same, above 0 when A is newer. }
function CompareVersions(A, B: String): Integer;
var
  I: Integer;
begin
  Result := 0;
  for I := 1 to 3 do
    if Result = 0 then
      Result := TakeNumber(A) - TakeNumber(B);
end;

{ Whether a program runs from one of the folder's environments (.venv and the rest): the window, the service, a model or an
  installer. The installed Lab's own update.py says, with the Lab's own Python, as update.bat checks. False when it cannot
  tell, as in a folder with no Lab's Python. }
function LabIsRunning(Folder: String): Boolean;
var
  Code: Integer;
begin
  Result := False;
  if not FileExists(Folder + '\python\python.exe') or not FileExists(Folder + '\tools\update.py') then
    Exit;
  if Exec(Folder + '\python\python.exe', '-c "import sys; sys.path.insert(0, sys.argv[1]); from tools import update; ' +
      'sys.exit(3 if update.lab_processes(sys.argv[1]) else 0)" "' + Folder + '"', Folder, SW_HIDE, ewWaitUntilTerminated,
      Code) then
  begin
    Log(Format('Whether the Lab runs from %s: exit code %d (3 is yes).', [Folder, Code]));
    Result := Code = 3;
  end;
end;

{ What the ready page says over an installed Lab, or '' over none. }
function UpdateText(Version: String): String;
begin
  Result := '';
  if Version = '' then
    Exit;
  if CompareVersions(Version, '{#AppVersion}') < 0 then
    Result := 'Setup is ready to update Fel''s Voice Lab in this folder from version ' + Version + ' to {#AppVersion}.'
  else if CompareVersions(Version, '{#AppVersion}') = 0 then
    Result := 'Setup is ready to install version {#AppVersion} again in this folder.'
  else
    Exit;
  Result := Result + #13#10#13#10 + 'Your voices, scenes, takes, models and settings stay as they are. Files the new ' +
    'version no longer has are removed, and the end of Setup says which models need installing again, if any.' + #13#10#13#10 +
    'Close the Lab before you go on.';
end;

{ Why Setup will not go on in this folder, or ''. }
function NewerInstalled(Version: String): String;
begin
  Result := '';
  if (Version <> '') and (CompareVersions(Version, '{#AppVersion}') > 0) then
    Result := 'This folder has version ' + Version + ' of Fel''s Voice Lab, which is newer than this Setup''s ' +
      '{#AppVersion}. Setup does not put an older version over a newer one, so nothing was changed.' + #13#10#13#10 +
      'To use {#AppVersion} anyway, uninstall the Lab first and keep your library, then run this Setup again.';
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
  Sync, Refusal: String;
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
    Refusal := NewerInstalled(LabVersionIn(RemoveBackslash(WizardDirValue)));
    if Result and (Refusal <> '') then
    begin
      SuppressibleMsgBox(Refusal, mbError, MB_OK, IDOK);
      Result := False;
    end;
  end;
end;

procedure CurPageChanged(CurPageID: Integer);
var
  Version, Text: String;
  Lines: TArrayOfString;
  I, Grown: Integer;
begin
  if CurPageID = wpReady then
  begin
    Version := LabVersionIn(RemoveBackslash(WizardDirValue));
    Text := UpdateText(Version);
    if Text <> '' then
    begin
      if CompareVersions(Version, '{#AppVersion}') < 0 then
      begin
        WizardForm.PageNameLabel.Caption := 'Ready to update';
        WizardForm.PageDescriptionLabel.Caption := 'From version ' + Version + ' to {#AppVersion}, keeping your library.';
      end
      else
      begin
        WizardForm.PageNameLabel.Caption := 'Ready to install again';
        WizardForm.PageDescriptionLabel.Caption := 'Version {#AppVersion} again, keeping your library.';
      end;
      { The label is one line high; make room for the paragraphs and move the list below it down. }
      Grown := WizardForm.ReadyLabel.Height;
      WizardForm.ReadyLabel.Caption := Text;
      WizardForm.AdjustLabelHeight(WizardForm.ReadyLabel);
      Grown := WizardForm.ReadyLabel.Height - Grown;
      WizardForm.ReadyMemo.Top := WizardForm.ReadyMemo.Top + Grown;
      WizardForm.ReadyMemo.Height := WizardForm.ReadyMemo.Height - Grown;
    end;
  end;
  { After an update, what update.py --after-setup found: the models to install again, if any. }
  if (CurPageID = wpFinished) and LoadStringsFromFile(ExpandConstant('{tmp}\update-summary.txt'), Lines) then
  begin
    Text := '';
    for I := 0 to GetArrayLength(Lines) - 1 do
      Text := Text + Lines[I] + #13#10;
    WizardForm.FinishedLabel.Caption := Text;
    WizardForm.AdjustLabelHeight(WizardForm.FinishedLabel);
    WizardForm.RunList.Top := WizardForm.FinishedLabel.Top + WizardForm.FinishedLabel.Height + ScaleY(12);
  end;
end;

{ Before anything is copied: the version in the folder, which must not be newer, and a Lab that must be closed. }
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Folder: String;
begin
  Folder := RemoveBackslash(WizardDirValue);
  InstalledVersion := LabVersionIn(Folder);
  Log('The Lab in ' + Folder + ' before copying: ' + InstalledVersion);
  Result := NewerInstalled(InstalledVersion);
  if (Result <> '') or (InstalledVersion = '') then
    Exit;
  while LabIsRunning(Folder) do
    if SuppressibleMsgBox('Fel''s Voice Lab is open. Close its window, stop serve.bat and any installer that runs from ' +
        Folder + ', then press Retry.', mbError, MB_RETRYCANCEL, IDCANCEL) <> IDRETRY then
    begin
      Result := 'The Lab is still open, so nothing was changed. Close it, then run Setup again.';
      Exit;
    end;
end;

{ Only retire a file if it still matches the installed release. Keep a copy with releases.json for --after-setup. }
function StageRetiredFile(Path, Version, Hash: String): Boolean;
var
  Source, Target: String;
begin
  Result := False;
  if InstalledVersion <> Version then
    Exit;
  Source := ExpandConstant('{app}\') + Path;
  if not FileExists(Source) then
    Exit;
  if Lowercase(GetSHA256OfFile(Source)) <> Hash then
    Exit;
  Target := ExpandConstant('{tmp}\retired\') + Path;
  Result := ForceDirectories(ExtractFileDir(Target)) and FileCopy(Source, Target, False);
  if Result then
    Log('Retiring unchanged file: ' + Path);
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
  { Record the files removed and bring the packages up to date so the core's check below loads the new version. }
  if (InstalledVersion <> '') and (CompareVersions(InstalledVersion, '{#AppVersion}') < 0) then
    if not SetUpStep('Finishing the update from ' + InstalledVersion + ' to {#AppVersion} ...',
        ExpandConstant('{app}\python\python.exe'),
        '"' + ExpandConstant('{app}\tools\update.py') + '" --after-setup --from ' + InstalledVersion + ' --setup-files "' +
        ExpandConstant('{tmp}\releases.json') + '" --wheels "' + ExpandConstant('{tmp}\wheels') + '" --summary "' +
        ExpandConstant('{tmp}\update-summary.txt') + '"') then
      Failed := 'finish the update (update.py --after-setup)';
  if not SetUpStep('Setting up the core: the Python packages Setup carries, so no internet is needed ...',
      ExpandConstant('{app}\python\python.exe'),
      '"' + ExpandConstant('{app}\tools\setup_core.py') + '" --wheels "' + ExpandConstant('{tmp}\wheels') + '"') then
    Failed := 'set up the core'
  else if not SetUpStep('Setting up the desktop window ...',
      ExpandConstant('{app}\.venv\Scripts\python.exe'),
      '"' + ExpandConstant('{app}\tools\setup_desktop.py') + '" --wheels "' + ExpandConstant('{tmp}\wheels') + '" --no-shortcut') then
    Failed := 'set up the desktop window';
  WizardForm.ProgressGauge.Style := npbstNormal;
  SetUpOk := Failed = '';
  if not SetUpOk then
    SuppressibleMsgBox('Setup copied the Lab but could not ' + Failed + '. What went wrong is at the end of ' +
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

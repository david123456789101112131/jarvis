; Inno Setup script for JARVIS (Inno Setup 6.3 or newer). scripts\build.ps1 runs it after PyInstaller:
;   ISCC.exe /DAppVersion=1.0.0 packaging\jarvis.iss
; It installs dist\JARVIS (the PyInstaller folder) for the current user: no administrator rights needed.
; JARVIS keeps its data in %LOCALAPPDATA%\JARVIS and its workspace in %USERPROFILE%\JARVIS; the uninstaller
; keeps both unless the user says otherwise, and never touches the workspace and projects.

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#define AppName "JARVIS"
#define AppExe "JARVIS.exe"
#define CliExe "jarvis-cli.exe"

[Setup]
AppId={{5E0C8B7A-3C2D-4F7B-9B8E-2A61D4C9F0A1}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=JARVIS
DefaultDirName={autopf}\JARVIS
DefaultGroupName=JARVIS
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=..\dist
OutputBaseFilename=JARVIS-Setup-{#AppVersion}
SetupIconFile=..\src\jarvis\assets\jarvis.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
ShowLanguageDialog=yes
; JARVIS is asked to quit (it saves unfinished tasks first) instead of being closed by Windows.
CloseApplications=no

[Languages]
Name: "hebrew"; MessagesFile: "compiler:Languages\Hebrew.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
hebrew.AutoStart=להפעיל את JARVIS עם הכניסה ל-Windows
english.AutoStart=Start JARVIS when I sign in to Windows
hebrew.SelfCheck=בדיקת מערכת של JARVIS
english.SelfCheck=JARVIS self-check
hebrew.DeleteData=למחוק גם את הזיכרון, ההגדרות, היומנים ומודלי הדיבור של JARVIS, ואת מפתחות ה-API שנשמרו?
english.DeleteData=Also delete JARVIS's memory, settings, logs and speech models, and the API keys it stored?
hebrew.KeepsProjects=הפרויקטים ותיקיית העבודה ב-%1 לא יימחקו.
english.KeepsProjects=Your projects and workspace in %1 are kept either way.

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "autostart"; Description: "{cm:AutoStart}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\dist\JARVIS\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\JARVIS"; Filename: "{app}\{#AppExe}"; AppUserModelID: "JARVIS"
Name: "{autoprograms}\{cm:SelfCheck}"; Filename: "{cmd}"; Parameters: "/k """"{app}\{#CliExe}"" check --full"""; WorkingDir: "{app}"; IconFilename: "{app}\{#AppExe}"
Name: "{autodesktop}\JARVIS"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon; AppUserModelID: "JARVIS"

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "JARVIS"; ValueData: """{app}\{#AppExe}"""; Tasks: autostart; Flags: uninsdeletevalue

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,JARVIS}"; Flags: nowait postinstall skipifsilent

[Code]
var
  DeleteData: Boolean;

{ A running JARVIS saves its unfinished tasks and quits, so its files can be replaced or removed. }
procedure StopJarvis();
var
  Code: Integer;
begin
  if FileExists(ExpandConstant('{app}\{#CliExe}')) then
    Exec(ExpandConstant('{app}\{#CliExe}'), 'stop', '', SW_HIDE, ewWaitUntilTerminated, Code);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  StopJarvis();
  Result := '';
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Code: Integer;
  Question: String;
begin
  if CurUninstallStep = usUninstall then
  begin
    StopJarvis();
    Question := CustomMessage('DeleteData') + #13#10#13#10 +
      FmtMessage(CustomMessage('KeepsProjects'), [ExpandConstant('{%USERPROFILE}\JARVIS')]);
    DeleteData := (not UninstallSilent()) and
      (MsgBox(Question, mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES);
    if DeleteData then
      Exec(ExpandConstant('{app}\{#CliExe}'), 'keys delete --all', '', SW_HIDE, ewWaitUntilTerminated, Code);
  end;
  if (CurUninstallStep = usPostUninstall) and DeleteData then
    DelTree(ExpandConstant('{localappdata}\JARVIS'), True, True, True);
end;

; Inno Setup script for the Athena ILI FFP Tool — per-user, no-admin install.
;
; Compile with:
;     "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" packaging\installer.iss
;
; This script expects PyInstaller to have already produced
;     dist\athena_ili_ffp\           (the one-folder distribution; CLI + GUI)
; The compiled installer lands at
;     dist\Setup_AthenaIliFfp_v0.3.8.exe

#define MyAppName        "Athena ILI FFP Tool"
#define MyAppVersion     "0.3.8"
#define MyAppPublisher   "Athena PowerTech LLP"
#define MyAppGuiExeName  "AthenaIliFfp.exe"
#define MyAppCliExeName  "athena_ili_ffp.exe"
#define MyAppShortName   "AthenaIliFfp"

[Setup]
; AppId — keep stable across versions so updates upgrade in place.
AppId={{6F19B7B4-3E27-4F8C-A5A1-3A0B7C6E1234}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} v{#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL=https://www.athenapowertech.com/
AppSupportURL=https://www.athenapowertech.com/
DefaultDirName={autopf}\Athena ILI FFP Tool
DefaultGroupName=Athena ILI FFP Tool
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=Setup_{#MyAppShortName}_v{#MyAppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern

; Per-user install — no admin rights required.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

; Use 64-bit install mode when on a 64-bit Windows so {autopf} resolves
; to the user's "Program Files" rather than "Program Files (x86)".
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible

; Show a setup summary and license-style page (using README as proxy).
LicenseFile=
InfoBeforeFile=

; Modern install/uninstall icons live next to the bundled .exe.
SetupIconFile=

; Display the GUI exe as the "main" program of this install — Windows
; uses it for the uninstall-list icon + Apps & Features entry.
UninstallDisplayIcon={app}\{#MyAppGuiExeName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
; Desktop shortcut for the GUI — default off per the v0.2 spec.
Name: "desktopicon";     Description: "Create a desktop shortcut for the GUI"; \
    GroupDescription: "Additional shortcuts:"; Flags: unchecked

; Optional CLI Start Menu entry (the GUI shortcut is always added).
Name: "startmenucmd";    Description: "Add 'Run FFP Analysis (CMD)' to Start Menu (for power users)"; \
    GroupDescription: "Additional shortcuts:"

; File association for .yaml files in the user's projects directory.
Name: "associateyaml";   Description: "Associate Athena project YAMLs with the GUI"; \
    GroupDescription: "File associations:"; Flags: unchecked

[Files]
; The whole PyInstaller one-folder distribution — CLI exe + GUI exe + DLLs + data.
Source: "..\dist\athena_ili_ffp\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

; Top-level docs accessible from the install directory.
Source: "..\README.md";                 DestDir: "{app}";       Flags: ignoreversion
Source: "..\docs\USER_GUIDE.md";        DestDir: "{app}\docs";  Flags: ignoreversion
Source: "..\docs\BUILD_INSTRUCTIONS.md";DestDir: "{app}\docs";  Flags: ignoreversion onlyifdoesntexist
Source: "..\docs\FFP_VALIDATION.md";    DestDir: "{app}\docs";  Flags: ignoreversion onlyifdoesntexist
Source: "..\docs\REPORT_FORMATS.md";    DestDir: "{app}\docs";  Flags: ignoreversion onlyifdoesntexist

; Seed the user-config area on first run (only if not already present —
; lets repeat installs preserve user-edited project YAMLs).
Source: "..\examples\kandla_project.yaml";         DestDir: "{userappdata}\Athena\ILI_FFP_Tool\projects"; Flags: onlyifdoesntexist
Source: "..\examples\hmel_ips1_ips2_project.yaml"; DestDir: "{userappdata}\Athena\ILI_FFP_Tool\projects"; Flags: onlyifdoesntexist

[Dirs]
; User data directory (project YAMLs + outputs + vendor profiles)
Name: "{userappdata}\Athena\ILI_FFP_Tool"
Name: "{userappdata}\Athena\ILI_FFP_Tool\projects"
Name: "{userappdata}\Athena\ILI_FFP_Tool\output"
Name: "{userappdata}\Athena\ILI_FFP_Tool\vendor_profiles"

[Icons]
; Start Menu — PRIMARY shortcut for the GUI (always installed).
Name: "{autoprograms}\{#MyAppName}\{#MyAppName}"; \
    Filename: "{app}\{#MyAppGuiExeName}"; \
    WorkingDir: "{app}"; \
    Comment: "Launch the Athena ILI FFP Tool"

; Start Menu — CLI shortcut (gated on the 'startmenucmd' task).
Name: "{autoprograms}\{#MyAppName}\Run FFP Analysis (CMD)"; \
    Filename: "{cmd}"; \
    WorkingDir: "{app}"; \
    Parameters: "/K title Athena FFP && echo Welcome to the Athena ILI FFP Tool && echo Type: athena_ili_ffp --help    to see options. && echo. && set PATH={app};%PATH%"; \
    Tasks: startmenucmd

; Start Menu — quick access to the install folder + docs + uninstall.
Name: "{autoprograms}\{#MyAppName}\Open install folder"; Filename: "{app}";        Comment: "Open the tool's install directory in Explorer"
Name: "{autoprograms}\{#MyAppName}\User Guide";          Filename: "{app}\docs\USER_GUIDE.md"
Name: "{autoprograms}\{#MyAppName}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"

; Desktop — GUI shortcut, opt-in via 'desktopicon' task.
Name: "{autodesktop}\{#MyAppName}"; \
    Filename: "{app}\{#MyAppGuiExeName}"; \
    WorkingDir: "{app}"; \
    Comment: "Launch the Athena ILI FFP Tool"; \
    Tasks: desktopicon

[Registry]
; .yaml file association (opt-in via 'associateyaml' task). Per-user
; registration under HKCU so it doesn't need admin and doesn't trample
; system-wide YAML associations (e.g. VS Code, Notepad++) when the user
; uninstalls.
Root: HKCU; Subkey: "Software\Classes\.yaml\OpenWithProgids"; \
    ValueType: string; ValueName: "AthenaIliFfp.ProjectYaml"; ValueData: ""; \
    Flags: uninsdeletevalue; Tasks: associateyaml

Root: HKCU; Subkey: "Software\Classes\AthenaIliFfp.ProjectYaml"; \
    ValueType: string; ValueName: ""; ValueData: "Athena FFP Project"; \
    Flags: uninsdeletekey; Tasks: associateyaml

Root: HKCU; Subkey: "Software\Classes\AthenaIliFfp.ProjectYaml\DefaultIcon"; \
    ValueType: string; ValueName: ""; ValueData: "{app}\{#MyAppGuiExeName},0"; \
    Tasks: associateyaml

Root: HKCU; Subkey: "Software\Classes\AthenaIliFfp.ProjectYaml\shell\open\command"; \
    ValueType: string; ValueName: ""; ValueData: """{app}\{#MyAppGuiExeName}"" ""%1"""; \
    Tasks: associateyaml

[Run]
; Post-install: offer to launch the GUI immediately.
Filename: "{app}\{#MyAppGuiExeName}"; \
    Description: "Launch {#MyAppName}"; \
    Flags: postinstall nowait skipifsilent

[UninstallDelete]
; Remove pyc / matplotlib font cache / chart-temp residues but PRESERVE
; the user's projects/output directories — those contain their work.
Type: filesandordirs; Name: "{app}"
; Note: we deliberately do NOT remove {userappdata}\Athena\ILI_FFP_Tool — that
; holds the user's project YAMLs, prior outputs, and vendor profiles.
; They can delete that folder manually if they want a full wipe.

; Inno Setup (jrsoftware.org/isinfo.php): Compile -> Output\AdSwitch-Setup.exe
[Setup]
AppId={{8F3C2B1A-5D47-4E8B-9A61-0D2E7C4B9F13}
AppName=AdSwitch
AppVersion=1.1.0
CloseApplications=yes
RestartApplications=no
DefaultDirName={autopf}\AdSwitch
DefaultGroupName=AdSwitch
OutputBaseFilename=AdSwitch-Setup
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=lowest
[Files]
Source: "dist\AdSwitch.exe"; DestDir: "{app}"; Flags: ignoreversion
[Icons]
Name: "{group}\AdSwitch"; Filename: "{app}\AdSwitch.exe"
Name: "{autodesktop}\AdSwitch"; Filename: "{app}\AdSwitch.exe"; Tasks: desktop
[Tasks]
Name: "desktop"; Description: "Ярлик на робочому столі"
[Run]
Filename: "{app}\AdSwitch.exe"; Description: "Запустити AdSwitch"; Flags: nowait postinstall skipifsilent

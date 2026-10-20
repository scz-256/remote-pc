; 远程电脑助手 —— 安装脚本（Inno Setup 6.3 或更新）
;
;   编译：python tools/build_installer.py
;   手工：iscc /DAppVersion=0.1.0 /DSourceDir=..\..\dist\remote-pc tools\installer\remote-pc.iss
;
; 为什么要有安装包：单文件 exe 被"拖到哪就在哪跑"，而它找不到配置时会
; **在 exe 旁边新建一份**（gui/paths.py 的查找顺序）。于是桌面上的 exe 会在
; 桌面上生出一个 config.toml、里面是一把全新密钥，而 MCP / CLI 还读着原来
; 那一份——两边静默分家，现象是"CC 忽然说 token 无效"，根本追不回来。
; 安装包把这件事变成确定的：程序装在一个固定目录里、配置就在它旁边、
; 桌面和开始菜单给的是快捷方式而不是 exe 的副本。
;
; 三条刻意的选择：
;
;   * **装成"目录版"（--onedir）。** 单文件版每次启动都要把 ~70 MB 解到
;     %TEMP%，1~3 秒；目录版铺好就完了，启动几乎瞬时。"一堆文件"这个复杂度
;     正是安装包该扛的。
;   * **默认按当前用户安装**（`PrivilegesRequired=lowest` → {app} 落在
;     %LOCALAPPDATA%\Programs 下）。不要管理员权限，而且那个目录**可写** ——
;     界面上的「随机生成密钥」要能把 config.toml 写回去，写不进去这个功能
;     就废了。装到 Program Files 也能用，但换密钥会失败（界面上会明说），
;     所以 [Code] 里那一步专门拦一下。
;   * **配置文件不删。** 卸载时留着 {app}\config.toml：重装回来还是同一把
;     密钥，对方手里的连接串不用换。（所以这里**没有**针对它的
;     [UninstallDelete]。）
;
; agent 那一头**不做安装包**：remote-agent.exe 的卖点是"一个文件丢过去双击
; 就跑，目标机器什么都不用装"，加一步安装等于把这个优点换掉。

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\..\dist\remote-pc"
#endif
#ifndef OutputDir
  #define OutputDir "..\..\dist"
#endif
#ifndef SetupIcon
  #define SetupIcon "..\..\assets\remote-pc.ico"
#endif
; 产物文件名不在这儿用宏包一层：build_installer.py 要按字面读它去拼路径。

[Setup]
; 别改这个 GUID：它是"同一个程序"的身份，改了会变成装两份、卸载卸不掉旧的。
AppId={{7C2F5A41-9D3E-4B18-A6C7-5E0B4D9F2A13}
AppName=远程电脑助手
AppVersion={#AppVersion}
AppVerName=远程电脑助手 {#AppVersion}
AppPublisher=远程电脑助手
DefaultDirName={autopf}\远程电脑助手
DefaultGroupName=远程电脑助手
DisableProgramGroupPage=yes
; 当前用户安装，不要管理员权限（见文件头第二条）。
PrivilegesRequired=lowest
OutputDir={#OutputDir}
OutputBaseFilename=远程电脑助手-安装程序
Compression=lzma2/max
SolidCompression=yes
; 安装程序自己的图标（exe、任务栏、安装向导左上角）。和主程序同一张图。
SetupIconFile={#SetupIcon}
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayName=远程电脑助手
UninstallDisplayIcon={app}\remote-pc.exe
#ifdef ChineseIsl
; 中文不是 Inno 自带语言包（官方只到土耳其语）。机器上有就编进去，
; 没有就退回英文界面——build_installer.py 事先探一下再把这个宏传进来。
ShowLanguageDialog=no
#endif

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
#ifdef ChineseIsl
Name: "chinesesimplified"; MessagesFile: "{#ChineseIsl}"
#endif

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："
Name: "shareconfig"; Description: "让命令行客户端和 MCP 也读这份配置（设置用户环境变量 REMOTE_PC_CONFIG）"; GroupDescription: "附加任务："

[Files]
; 整个 dist/remote-pc/ 铺进安装目录。exe 和它旁边的 _internal 必须一起走。
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\远程电脑助手"; Filename: "{app}\remote-pc.exe"
Name: "{group}\卸载远程电脑助手"; Filename: "{uninstallexe}"
Name: "{autodesktop}\远程电脑助手"; Filename: "{app}\remote-pc.exe"; Tasks: desktopicon

[Registry]
; 让 `python -m server.cli` / `server.mcp_server` / 界面三方读**同一个**文件。
; server/config.py 和 gui/paths.py 都优先认这个环境变量，所以这一条之后
; "配置文件到底在哪"就没有第二个答案了 —— 那正是这个安装包要解决的问题。
Root: HKCU; Subkey: "Environment"; ValueType: string; ValueName: "REMOTE_PC_CONFIG"; \
    ValueData: "{app}\config.toml"; Flags: uninsdeletevalue; Tasks: shareconfig

[Run]
; 播种配置。**让程序自己写**，不在安装脚本里拼一份模板——模板只有
; gui/toml_edit.py 那一份，两边各写一份早晚对不上。已经有配置就原样不动。
Filename: "{app}\remote-pc.exe"; Parameters: "--init-config --config ""{app}\config.toml"""; \
    Flags: runhidden; StatusMsg: "生成配置文件（config.toml）…"
Filename: "{app}\remote-pc.exe"; Description: "启动远程电脑助手"; \
    Flags: nowait postinstall skipifsilent

; 这里**没有** [UninstallDelete]：卸载会带走它装进去的每一个文件，而
; 运行时才生成的 config.toml 和 logs\ 会留下（Inno 默认就不动它们）。
; 这是故意的——重装回来还是同一把密钥，对方手里的连接串不用换。

[Code]
{ 装到一个写不进去的目录（典型：Program Files）会让「随机生成密钥」永远失败。
  与其等用户点了按钮才发现，不如在选目录这一步就说清楚。 }
function NextButtonClick(CurPageID: Integer): Boolean;
var
  Target, Probe: String;
begin
  Result := True;
  if CurPageID <> wpSelectDir then
    Exit;

  Target := WizardDirValue;
  if not ForceDirectories(Target) then
  begin
    MsgBox('建不了这个目录：' + Target, mbError, MB_OK);
    Result := False;
    Exit;
  end;

  Probe := AddBackslash(Target) + '.write-test.tmp';
  if SaveStringToFile(Probe, 'x', False) then
    DeleteFile(Probe)
  else
  begin
    MsgBox('这个目录写不进去：' + Target + #13#10#13#10 +
           '密钥就存在这个目录里的 config.toml，程序要能改它（界面上的' +
           '「随机生成密钥」）。装到 Program Files 这类系统目录的话，' +
           '那个按钮会一直失败。' + #13#10#13#10 +
           '建议换成自己的目录，比如 D:\远程电脑助手；' +
           '或者直接用默认的（当前用户的 AppData 下，不需要管理员权限）。',
           mbError, MB_OK);
    Result := False;
  end;
end;

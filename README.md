# 远程电脑助手（remote-pc）

局域网内的远程协助工具。操作端运行 hub 和图形工作台，被控端运行单个 agent；
被控端主动向外建立 WebSocket 连接，操作端在对方知情、可见、可随时中断的前提下
执行命令、传输文件、截屏、读写剪贴板，也可通过 Claude Code 的 MCP 工具或命令行调用。

- **仅出站连接**：被控端不监听任何端口，无需修改防火墙。
- **操作可见**：每条命令都显示在被控端窗口；危险命令必须由本机用户确认。
- **随时断开**：关闭被控端窗口即断开，不安装服务、不设置自启动、不隐藏进程。
- **双向审计**：两端分别记录命令、拒绝原因和命中的规则。

## 功能

| 功能 | 说明 |
| --- | --- |
| 命令执行 | 支持 PowerShell / pwsh / cmd / bash，多会话并发，可读写 stdin、中断、强制结束 |
| 文件传输 | 分片传输并做 SHA-256 校验 |
| 截屏 | 获取被控端全屏画面 |
| 剪贴板 | 读取或写入被控端剪贴板 |
| 实时消息 | 两端图形界面之间发送文字，仅本次会话可见，不写入日志 |
| 策略控制 | `safe` / `confirm` / `auto` 三种模式，可限制操作目录 |
| 审计 | hub 与被控端各保留一份 JSONL 日志 |

## 工作原理

```
操作端                                     被控端
Claude Code / CLI / GUI                    agent.py 或 remote-agent.exe
      |                                          ^
      v                                          |
 server.mcp_server / server.cli                 | WebSocket（被控端发起）
      |                                          |
      v                                          |
 server.hub  <-------- WebSocket --------------+
 :8765
```

hub 默认监听 `0.0.0.0:8765`。被控端使用预共享密钥完成 HMAC 挑战-应答后接入，
密钥不会出现在网络流量中。

## 快速开始

### 环境

```bash
pip install -r requirements.txt
```

### 操作端

```bash
python tools/gen_token.py    # 生成 config.toml，并打印被控端连接命令
python -m server.hub
```

首次启动时，Windows 防火墙会询问是否允许 8765 端口，请选择“专用网络”。
如未放行，可用管理员 PowerShell 添加规则：

```powershell
netsh advfirewall firewall add rule name="remote-pc hub" dir=in action=allow ^
  protocol=TCP localport=8765 profile=private
```

### 被控端

将 `agent/agent.py` 或 `tools/build_agent.py` 生成的 `remote-agent.exe` 复制到被控端，
然后运行：

```powershell
python agent\agent.py --server ws://192.168.1.23:8765 --token <密钥> --mode confirm
```

被控端窗口会显示连接地址、模式、审计日志路径和会话码。请与操作端核对会话码，
确认连接对象无误。

### 验证

```bash
python -m server.cli status
```

输出应包含被控端主机名、在线会话和会话码。

## 图形界面

```bash
python app.py       # 源码运行
remote-pc.exe       # 打包版
```

同一个程序包含两种角色：

- **服务器端**：生成密钥、启停 hub、查看连接与审计状态，并与被控端收发消息。
- **客户端**：填写连接串 `ws://地址:端口#密钥` 连接 hub；连接前可点击“检测”
  逐项检查端口、密钥、网段与在线设备。

服务器端按以下顺序查找 `config.toml`，先找到者生效：

1. `--config <路径>`
2. 环境变量 `REMOTE_PC_CONFIG`
3. 程序所在目录
4. 程序目录向上 1–3 层
5. `%LOCALAPPDATA%\remote-pc\server.toml`
6. 均未找到时新建（优先程序目录，不可写则使用用户目录）

“随机生成密钥”会原子替换 `config.toml`，仅修改 `token` 一行，并保留
`config.toml.bak`。写入失败时不会回退到其他位置，界面会显示具体原因。

实时消息仅在双方均使用图形界面时可用，单条上限 2000 字符；记录只存在于
当前窗口，不写入审计或活动日志。

## 安全机制

- **仅出站连接**：被控端不开放端口。
- **密钥不传输**：双方基于预共享密钥做 HMAC 挑战-应答。
- **会话码**：两端显示同一会话码，用于人工确认。
- **网段限制**：hub 通过 `allowed_networks` 限制允许接入的来源。
- **路径限制**：被控端可用 `--allow-root` 将文件操作和 `cd` 限制在指定目录。
- **三道确认**：Claude Code 权限、hub 策略检查、被控端策略与系统置顶对话框。

### 策略模式

| 模式 | 行为 |
| --- | --- |
| `safe` | 仅允许只读命令 |
| `confirm` | 默认。黑名单拒绝，灰名单弹窗确认，其余放行 |
| `auto` | 黑名单拒绝，其余放行，不弹窗 |

危险命令的确认框默认选择“否”，超时视为拒绝，不提供“全部同意”选项。

### 审计

- 操作端：`logs/audit-YYYYMMDD.jsonl`
- 被控端：默认 `agent/agent-audit.jsonl`，exe 版位于 exe 同目录

聊天正文不写入任何日志。

## 配置

### 服务端 config.toml

```toml
[server]
token = "<预共享密钥>"
host = "0.0.0.0"
port = 8765
allowed_networks = ["127.0.0.0/8", "192.168.1.0/24"]
mode = "confirm"
exec_default_timeout_s = 120
single_agent_id = true
```

可用环境变量覆盖：`REMOTE_PC_CONFIG`、`REMOTE_PC_TOKEN`、`REMOTE_PC_PORT`、
`REMOTE_PC_HUB_URL`、`REMOTE_PC_MODE`。

### 被控端参数

| 参数 | 说明 |
| --- | --- |
| `--server` | hub 地址，如 `ws://192.168.1.23:8765` |
| `--token` | 预共享密钥 |
| `--mode` | `safe` / `confirm` / `auto`，默认 `confirm` |
| `--allow-root` | 文件操作与 `cd` 的根目录 |
| `--shell` | `powershell`（默认）/ `pwsh` / `cmd` / `bash` |
| `--confirm-timeout` | 确认框超时秒数，默认 60 |
| `--max-lifetime` | 最长连接时间，到时自动退出 |
| `--name` | 被控端标识，多台设备时用于区分 |
| `--config` | 读取 TOML 配置 |

示例见 `agent/config.example.toml`。

## 命令行

```bash
python -m server.cli status
python -m server.cli exec "ipconfig"
python -m server.cli output <session> --since <seq>
python -m server.cli stdin <session> "y\n"
python -m server.cli interrupt <session>
python -m server.cli kill <session>
python -m server.cli put <本地路径> <远端路径>
python -m server.cli get <远端路径> <本地路径>
python -m server.cli shot
python -m server.cli clip-get
python -m server.cli clip-set "文本"
```

`exec --timeout` 到期只结束等待，命令仍在被控端运行，可继续用 `output`、
`stdin`、`interrupt` 或 `kill` 操作。

## Claude Code / MCP

```bash
claude mcp add remote-pc -s project \
  -e "PYTHONPATH=<项目目录的绝对路径>" \
  -- python -m server.mcp_server
```

MCP 提供以下工具：`remote_status`、`remote_exec`、`remote_session_output`、
`remote_write_stdin`、`remote_interrupt`、`remote_kill`、`remote_put_file`、
`remote_get_file`、`remote_screenshot`、`remote_clipboard_get`、
`remote_clipboard_set`。

## 打包

```bash
pip install pyinstaller
python tools/build_app.py            # dist/remote-pc.exe，单文件，双角色
python tools/build_app.py --onedir   # 目录版，启动更快
python tools/build_agent.py          # dist/remote-agent.exe
python tools/build_installer.py      # 安装包，需要 Inno Setup
```

## 项目结构

```
app.py            程序入口
agent/            被控端（单文件、无第三方依赖）
server/           hub、协议、策略、审计、CLI、MCP
gui/              PySide6 图形工作台
assets/           图标
tools/            密钥生成与打包脚本
```

## 运行边界

本工具不安装系统服务，不设置开机启动，不提供隐藏或静默模式，也不驻留托盘。
被控端始终以可见窗口运行，关闭窗口即断开连接。所有连接仅面向局域网使用。

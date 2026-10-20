# 远程电脑

让我这边的 Claude Code 能在**同学的电脑**上执行命令、传文件、截屏、读写剪贴板。

前提：**对方知情且同意**，他那边随时看得见每条命令，随时能关掉窗口断开。

---

## 它长什么样

```
   我的电脑（hub）                          同学的电脑（agent）
 ┌──────────────────────┐                ┌──────────────────────┐
 │  Claude Code         │                │                      │
 │    ↓ MCP             │                │   一个黑框窗口        │
 │  server/mcp_server   │                │   实时滚动收到的命令  │
 │    ↓ HTTP            │                │   危险命令弹窗问他要  │
 │  server/hub  ────────┼─── WebSocket ──┼──→ agent.py          │
 │   :8765              │   ← 由他发起   │                      │
 └──────────────────────┘                └──────────────────────┘
```

**连接方向是反的**：由同学的电脑主动连出来。

（也可以完全不打命令行：`tools/build_app.py` 打出来的 `remote-pc.exe`
是**同一个文件、两种角色**，双击选一下就行，见下面「图形界面」一节。）

这样一来：

- 他的电脑上**不开任何监听端口**，不需要为这个工具改防火墙
- 他的**公网 IP 暴露不暴露都无所谓**，反正没有入口
- 他**把窗口一关，连接立刻断**，不需要我配合做任何事
- 他电脑重启 / 断网 / 拔网线，都只是连接断掉，不会留下任何常驻的东西

---

## 三步跑起来

### 1. 我这边：生成密钥、起 hub

```bash
pip install -r requirements.txt
python tools/gen_token.py          # 生成密钥，写入 config.toml，并打印对方要用的命令
python -m server.hub
```

> 走图形界面的话，`python app.py` → 选「服务器端」→ 点**「随机生成密钥」**，
> 和 `gen_token.py --force` 是同一件事，而且会告诉你密钥写进了哪个文件、
> 那个路径是怎么找出来的。（换完密钥 agent 要**重开**，见「出问题先看这里」。）
> `--force` 也是**只换 `token` 那一行**——你自己写的注释、调过的
> `allowed_networks` 都会原样留着。

`gen_token.py` 会顺便探测本机局域网 IP，直接把对方要跑的那行命令打印出来：

```
token:
  <gen_token.py 当场打印的那把密钥，这里不写真的>

让同学在他的电脑上跑（把 IP 换成你这边局域网地址）：
  python agent\agent.py --server ws://192.168.1.23:8765 --token <密钥> --mode confirm

我这边启动 hub：
  python -m server.hub

注意：hub 监听 8765，第一次运行 Windows 可能会弹防火墙提示，选「专用网络」允许。
```

第一次起 hub 时 Windows 也可能弹防火墙提示，选**「专用网络」允许**。
要是当时手滑点了取消，或者局域网里还是连不上，手动加一条入站规则
（**管理员** PowerShell）：

```powershell
netsh advfirewall firewall add rule name="remote-pc hub" dir=in action=allow ^
  protocol=TCP localport=8765 profile=private
```

`profile=private` 表示只在"专用网络"生效。用公共 WiFi 时别开这个口子。

### 2. 他那边：跑 agent

把 `agent/agent.py`（或 `tools/build_agent.py` 打出来的 `remote-agent.exe`）拷给他，
让他**双击运行**，或者：

```powershell
python agent\agent.py --server ws://192.168.1.23:8765 --token <密钥> --mode confirm
```

他的窗口长这样：

```
====================================================================
  远程终端 agent
  本机    : DESKTOP-QU8NFQ6  (ASUS)
  服务端  : ws://192.168.1.23:8765/agent
  模式    : confirm
  审计日志: E:\...\agent\agent-audit.jsonl
  每条命令都会打印在这里；危险命令会弹窗问你。
  按 Ctrl+C 立即退出。
====================================================================
[10:27:43] 已连接到 ws://192.168.1.23:8765/agent
[10:27:43] 会话码 045387 —— 对方屏幕上应该显示同样的数字，对不上就别让他继续操作
[10:27:43] 模式 confirm，默认 shell powershell，按 Ctrl+C 断开退出
[10:27:44] $ ipconfig
[10:27:44]   -> 结束，退出码 0（0.2s），当前目录 C:\Users\ASUS
```

**会话码**念给对面核对一下，两边一致才说明连的确实是本人。

### 3. 我这边：确认能连上

```bash
python -m server.cli status
```

看到对方的机器名和会话码就通了。**会话码两边应该一致**——这是用来确认
"连的确实是我同学那台"的，不一致就说明有人的密钥泄露了。

---

## 图形界面：同一个 exe，两种角色

图形界面只有 PySide6 这一版：启动后进入统一工作台，左侧可以在「概览」「服务器端」
「客户端」之间切换，连接信息、服务状态和实时事件集中显示。没有"没有 PySide6
就退回 Tk"这种开关——PySide6 是硬依赖。

以前还有个「内置终端」页面（Windows 伪终端里跑一个本机 `cmd.exe`，外加「复制 MCP
配置」/「复制项目地址」两个按钮），**2026-10-20 删掉了**：那个伪终端跑 `claude`、
`codex` 不如直接在 CMD 里顺手，为它还得多背一个 `winpty`。

MCP 那套现在不经过界面：用 `claude mcp add` 配一下就行（见下文「接进 Claude Code」），
和人手拷一段 JSON 相比没有区别。

上面那三步是命令行跑法，一直都在。但它分发起来有两处难受：同学的电脑得装
Python，还得从 `ipconfig` 里自己找出我的 IPv4；而且**他只看得见命令，看不见
返回内容**（agent 只打印 `$ 命令` 和一行"结束，退出码 0"）。

`python tools/build_app.py` 打出来的 `remote-pc.exe` 就是冲这两件事去的：

- **一个文件、两种角色。** 双击先选「服务器端」还是「客户端」，选错了用 × 关掉重开。
  目标机器**不需要装 Python**。
- **服务器端**：本机 IPv4 大字显示、**「随机生成密钥」**（生成即写回同一个
  `config.toml`，原子替换 + `.bak`，只动 `token` 那一行）、解析到的配置文件路径
  **和它是怎么被找到的**一并写出来、启动服务后自动跑一次本机自检。
  再往下是工具箱：命令行输入框（不开 CC 也能直接发命令）、传文件 / 取文件 /
  截屏 / 读剪贴板 / 写剪贴板。
- **客户端**：把两个方向都摊开——收到的命令（加粗）、策略判定、stdout、stderr。
  「我全程看得见」这句话这才算做实。

### 给同学讲清楚：两个窗口

他那边跑起来是**两个窗口**：

| 窗口 | 是什么 |
| --- | --- |
| 黑框（控制台） | 永久滚动记录：连上/断开、每条命令、策略判定的结果 |
| 图形窗口 | 结构化的双向流量：收到的命令、完整输出、报错 |

**关掉任意一个都等于断开。** 黑框的标题栏一直写着这句话——打印出来会被日志
滚走，挂在标题栏上才不会。

> 走图形界面（`--role client`）时只有那一个窗口，**没有黑框**——那时候 agent 在
> PySide 进程里跑，自己不带控制台，于是 Windows 会给**每一条远程命令**新开一个
> 控制台窗口，在对方屏幕上闪一下；截屏和读写剪贴板也是同一个坑。所以
> `agent/agent.py::child_creationflags()` 在"自己没有控制台"时给子进程加
> `CREATE_NO_WINDOW`。**对方看得见的那份记录没有因此少**：`say()` 照样把
> `$ 命令原文` 打进 agent 自己的黑框（`remote-agent.exe` 那条路），加上 audit 日志。
> 被藏起来的只是每条命令临时闪一下的子窗口。
>
> 只在"自己没有控制台"时才加标志，是有讲究的：有控制台时子进程直接继承它、
> 本来就不弹窗口，而且那种情况下 `CTRL_BREAK` 送得到，能优雅地中断远程命令。
> 没有控制台时那个调用本来就送不到（走的是"发不出去，直接强杀"那一支），
> 所以这一下是零代价。

### 实时消息：两边直接说话

「服务器端」和「客户端」两个页面底部各有一张**消息**卡片，双方输入文字即时送达——
"你要改的那个程序在哪"这类话不用再绕微信，也不会混进命令记录。

- **只在双方都用图形界面时可用。** 对方跑的是 `remote-agent.exe`（黑框版）时，
  服务器端发送会得到「对端为命令行 agent，不支持消息」；反过来，如果 hub 是用
  `python -m server.hub` 起的命令行版，客户端的输入框保持禁用并注明原因。
- **记录只保留在本次会话。** 关掉窗口即清空，不写文件、不写 `agent-audit.jsonl`，
  也不进「活动日志」/「实时事件」——聊天和操作记录是两回事。
- **单条上限 2000 字符**，输入框是单行，回车发送。
- 窗口不在前台时，收到消息只**闪任务栏**，不弹窗、不出声。

消息走客户端那条已有的 WebSocket（`chat` / `chat_server` 两种帧），不新开端口、
不改协议版本：旧版 agent 收到不认识的帧只会忽略，不会断线。

### 三种开场

```bash
remote-pc.exe                      # 起工作台，落在概览页（双击就是这个）
remote-pc.exe --role server        # 直接当服务器端
remote-pc.exe --role client --server ws://192.168.1.23:8765 --token <密钥>
remote-pc.exe --server ws://… --token … --mode confirm
                                   # 命令行直通：和以前的 remote-agent.exe 一模一样
```

判据很简单：**带了 `--role` 就是图形界面**；否则只要出现
`--server/--token/--mode/…` 里任何一个（或者 `-h`），就**原样转交**给命令行 agent，
一个参数都不解释、不补充。两个都没有 = 双击 = 进工作台。**旧的用法一个字都没变。**

> 最后那条是唯一会自己开黑框的：exe 默认不带黑框打（见「打包成 exe」），
> 但这一趟就是命令行 agent，对方得有黑框才看得见每条命令，
> 所以 `ensure_streams()` 会自己 `AllocConsole()` 补一个——**行为跟以前一样**。

客户端那边粘贴一次连接串就够了（`ws://地址:端口#密钥`，整条命令、整段聊天记录也行）。
**只有握手成功之后才会记住**——密钥打错永远不会被记住，等于白送一个
"这一串确实连上过"的校验。记住的东西放在 `%LOCALAPPDATA%\remote-pc\client.json`，
**不放 exe 旁边**：同学的 exe 可能在 U 盘上，而里面有密钥。

连接按钮旁边还有一个**「检测」**：不建立连接，只探一次 `/api/status`，
把"端口通不通 / 密钥对不对 / 会不会被 `allowed_networks` 拒掉 / 对方有几台在线"
逐条列出来。它是只读的，**没连接的时候随便点**，出错时也不必先真的连一次。
从上次记录里带进来的地址会在日志里注明「这是历史记录，不代表现在还连得上」——
因为那串填在输入框里看起来和你刚粘进去的一模一样。

客户端**只在未连接时**才能改模式，往松的方向改（`→ auto`）会再问一次。

### 配置到底读哪个文件

服务器端启动前按这个顺序找 `config.toml`，**先中先得**，界面上会把它显示出来：

| 顺序 | 位置 |
| --- | --- |
| 1 | `--config <路径>` |
| 2 | 环境变量 `REMOTE_PC_CONFIG` |
| 3 | exe 所在目录的 `config.toml` |
| 4 | 从 exe 目录**往上找 1~3 层** |
| 5 | `%LOCALAPPDATA%\remote-pc\server.toml` |
| 6 | 都没有 → 新建一份（先程序旁边，不可写才退到用户目录） |

> **MCP 为什么还连得上？**
> 因为 `dist\remote-pc.exe` 在第 4 步会往上找到**仓库根的那份 `config.toml`**——
> 也就是 `python -m server.hub`、`server.cli`、MCP server 一直在读的那一份。
> 在界面里改密钥，写回去的是**同一个文件**，不会凭空多出第二把密钥。
> 日志目录同理：绝不放在单文件 exe 解压出来的临时目录里（那个目录**退出就删**，
> 审计日志放那儿等于没留）。

服务器端页面上的**「随机生成密钥」**：点一下立即生成、立即落盘，并把
「写进了哪个文件、这个路径是怎么找到的」写在下面一行里。写回是
**原子替换 + 留一份 `config.toml.bak`**，而且**只改 `token` 那一行**——
你自己写的注释、调过的 `allowed_networks` 和 `exec_default_timeout_s`
一个字节都不动。直接**手改**输入框里的密钥也一样会落地（失焦或回车时写回），
空的不会写——那是"还没打完"，不是"我不要密钥了"。

写不进去（目录只读、文件不是 UTF-8、里面没有 `[server]` 段）时**不会**
退到别的地方去写：界面红字写出原因，并明说"密钥只在本次会话里有效，
配置文件没动"。宁可什么都不写，也不能凭空多出第二把密钥——
那种故障几天后才以"CC 忽然说 token 无效"的形式冒出来，很难往回追。

**换密钥是个全局动作**，同一时刻只该有一把，而这把密钥有四个持有者：

| 谁 | 什么时候读的 |
| --- | --- |
| 界面输入框 | 实时 |
| `config.toml` | 点下去立刻写回 |
| 正在跑的 hub | 由 `HubControl.apply()` 推过去，**新连接立刻生效，不用重启** |
| CC / MCP / CLI 那些长命的进程 | 手里揣着旧的，**收到 401 时会自己重读一遍配置再试一次** |

所以 hub 正跑着的时候点这个按钮会先问一句，把代价说清楚：**已经在线的设备不会
掉线**（鉴权只在连接时做一次），但它们一旦断线重连就会被拒——得把新的连接串
重新发给对方。不做成禁用：密钥真泄露的时候你正需要换它，禁用等于把救生门焊死。

### 安全上没松口的地方

- 危险命令**仍然是系统置顶对话框**，默认"否"、超时"否"。没有换成窗口内的
  是/否按钮，没有"本次会话全部同意"，没有任何绕过开关。
- 图形窗口**绝不设 `-topmost`**——否则它可能盖住那个否决权。
- 客户端窗口里**没有**托盘、没有最小化到后台、没有自启、没有静默模式。
- 关掉服务器端窗口 = 停 hub = 对方掉线（会先问一句；正在传的文件最多让等 10 秒）。

---

## 接进 Claude Code

```bash
claude mcp add remote-pc -s project \
  -e "PYTHONPATH=<项目目录的绝对路径>" \
  -- python -m server.mcp_server
```

（把 `<项目目录的绝对路径>` 换成本机上这个文件夹的位置。）

`-s project` 的作用域只有这个仓库：在别的目录打开 CC，`remote_*` 工具不会出现。
想在**任何目录**下都能用（比如在自己的项目里让 CC 顺手看着同学那台电脑），
把 `-s project` 换成 `-s user` 注册一次即可。`config.toml` 是按包位置找的，
和当前在哪个目录无关。

之后在 CC 里直接说话就行：

> 看他电脑的 IP 是多少
> 在他电脑上装一下 requests
> 截个屏我看看他那个程序弹出来没有

CC 会调 `remote_*` 系列工具。**每一步都还会走 CC 自己的权限确认**，
加上 agent 那边的策略，总共三道关。

### 暴露出来的工具

| 工具 | 作用 |
| --- | --- |
| `remote_status` | 对方在不在线、机器信息、各会话状态 |
| `remote_exec` | 执行一条命令（可指定 session / cwd / shell / 超时） |
| `remote_session_output` | 读某个会话的输出。**要传 `since_seq`** —— 结果末尾的 `seq=N` 就是下次该传的值；不传的话会把该会话自建立起的所有输出一起拿回来 |
| `remote_write_stdin` | 往正在跑的命令喂输入（`y\n`、`input()` 那种） |
| `remote_interrupt` | 中断当前命令（相当于对面按 Ctrl+Break） |
| `remote_kill` | 强杀当前命令连同子进程 |
| `remote_put_file` / `remote_get_file` | 传文件（分片 + sha256 校验） |
| `remote_screenshot` | 截对面全屏 |
| `remote_clipboard_get` / `remote_clipboard_set` | 读/写对面剪贴板 |

---

## 安全模型

### 密钥不上网

握手不是"我把密钥发给你"，是**挑战-应答**：

1. hub 发一个随机 nonce
2. agent 回 `HMAC-SHA256(密钥, nonce)`
3. hub 本地算一遍，比对

密钥本身从不出现在网络流量里。而且每次握手的 nonce 都不同，
**抓包重放也没有用**。

### 三道关

| 关卡 | 在哪 | 拦什么 |
| --- | --- | --- |
| ① Claude Code 权限 | 我的电脑 | 我还没点"允许"的工具调用 |
| ② hub 策略检查 | 我的电脑 | 发送前先判一遍，防止 LLM 手滑 |
| ③ **agent 策略 + 弹窗** | **同学的电脑** | **最终否决权在他手里** |

第 ③ 关是真正算数的那个：前两关都在我的机器上跑，我说了算；
但命令要在**他**的机器上执行，得**他**点"是"。

两边的策略表是同一份（`server/policy.py` 和 `agent/agent.py` 里的副本）。
改一边就必须同步另一边——漏了会出现"hub 拦下、agent 却放行"的缺口。

### 三种模式

| 模式 | 行为 | 什么时候用 |
| --- | --- | --- |
| `safe` | 只放行只读命令，其余一律拒 | 刚接通，先验证链路 |
| `confirm` | **默认**。黑名单直接拒，灰名单弹窗问，其余放行 | 日常 |
| `auto` | 黑名单直接拒，其余全放行 | 基本别用 |

黑名单（任何模式下都拒，连弹窗都不弹）：格式化磁盘、`diskpart`、
改启动项、删卷影副本、清事件日志、新建系统账户、关防火墙、
删 HKLM 注册表、关 Defender、递归删系统目录。

灰名单（`confirm` 模式下弹窗）：关机重启、强杀进程、删/移文件、
改权限、写开机自启、建计划任务、装软件、`git push`、管道执行远程脚本。

弹窗长这样，**默认按钮是"否"**，超过 `--confirm-timeout` 秒不响应也算拒绝：

```
┌─ 远程指令确认 ────────────────────────────┐
│  ⚠ 来自局域网的另一台电脑，想执行下面的命令：│
│                                            │
│  del important.txt                         │
│                                            │
│  原因：删除操作                             │
│                                            │
│  只有你点「是」才会执行。                   │
│                                            │
│              [ 是 ]  [ 否 ]                │
└────────────────────────────────────────────┘
```

> 这层是**启发式正则，不是安全边界**。它拦的是常见的手滑
> ——比如 LLM 把 `rm -rf build` 写成 `rm -rf /`，或者在错误的工作目录下删除。
> 拦不住蓄意绕过。真正的边界是"对面那个人看得见每一条命令，随时能点否"。

### 审计日志

两边各记一份，任何时候都能翻：

- `logs/audit-YYYYMMDD.jsonl`（hub 侧）：每条命令、拒绝原因、命中的正则
- `agent/agent-audit.jsonl`（agent 侧）：同样内容，**在他自己的机器上**

用 exe 跑的时候，agent 侧的日志落在 **exe 旁边**（`remote-agent.exe` 同目录），
启动横幅里会写明完整路径。这一点是特意处理的：单文件 exe 的 `__file__`
指向解压出来的临时目录，那个目录**退出就删**，日志放那儿等于没留。
exe 所在目录不可写时（比如装在 `Program Files`）退到
`%LOCALAPPDATA%\remote-agent\`。

**「消息」里的聊天正文不在上面这两份日志里。** 它只存在于两个窗口的界面上，
关掉即消失——审计记录的是"谁在什么时候执行了什么"，不是两个人说了什么。

```json
{"event": "confirm", "session": "main", "cmd": "del a.txt",
 "reason": "删除操作", "approved": false, "cwd": "C:\\Users\\ASUS"}
```

---

## 命令行工具

MCP 出问题的时候（或者不想开 CC 的时候）直接用它：

```bash
python -m server.cli status                        # 对方在线吗
python -m server.cli exec "ipconfig"               # 跑一条
python -m server.cli exec "python app.py" --session app --timeout 5   # 超时就返回，命令继续跑
python -m server.cli output app                    # 看它跑到哪了（不加 --since 会把该会话的全部历史都打出来）
python -m server.cli output app --since 42         # 只看序号 42 之后的新输出
python -m server.cli stdin app "y\n"               # 喂一行给它
python -m server.cli interrupt app                 # Ctrl+Break
python -m server.cli kill app                      # 强杀
python -m server.cli put ./setup.ps1 "C:\Users\me\setup.ps1"
python -m server.cli get "C:\Users\me\out.log" ./out.log
python -m server.cli shot                          # 截屏
python -m server.cli clip-get
python -m server.cli clip-set "hello"
```

`exec` 的 `--timeout` 到点只是**返回**，命令还在对方机器上跑——
接着用 `output` / `stdin` / `kill` 操作它。

### agent 的参数

| 参数 | 说明 |
| --- | --- |
| `--server` | hub 地址，`ws://192.168.1.23:8765` |
| `--token` | 预共享密钥 |
| `--mode` | `safe` / `confirm` / `auto`，默认 `confirm` |
| `--allow-root` | 只允许在这个目录下操作文件和 `cd`（路径牢笼） |
| `--shell` | `powershell`（默认）/ `pwsh` / `cmd` / `bash` |
| `--confirm-timeout` | 弹窗等多少秒算拒绝，默认 60 |
| `--max-lifetime` | 最多连多久自动退出 |
| `--name` | 这台机器的标识，多台时用来区分 |

也可以写成配置文件（见 `agent/config.example.toml`），用 `--config` 指定。

---

## 打包成 exe

```bash
pip install pyinstaller
python tools/build_app.py         # -> dist/remote-pc.exe    推荐：一个文件两种角色
python tools/build_app.py --console   # 同上，但带黑框（排查启动崩溃时用）
python tools/build_app.py --onedir    # -> dist/remote-pc/      目录版，启动快
python tools/build_agent.py       # -> dist/remote-agent.exe  14 MB，只含客户端

winget install JRSoftware.InnoSetup   # 只要一次（装的是编译器 iscc）
python tools/build_installer.py   # -> dist/远程电脑助手-安装程序.exe  52 MB
```

`build_installer.py` 会先检查 `dist/remote-pc/`（没有或者 `--force-build` 才重打那次
一分多钟的目录版），再找 `iscc` 编译。**没装 Inno 不会让脚本失败**：它会打印
`winget install JRSoftware.InnoSetup` 然后退回 0——因为目录版本身已经是能用的产物。

**单文件还是目录**：默认单文件，70 MB，拷到哪台机器上都能跑。代价是**每次启动**都要把它
解到 `%TEMP%`（实测 1~3 秒，慢盘更久），退出时再删掉。`--onedir` 是 163 MB 的一堆
文件，铺在目录里、启动几乎瞬时，但分发的是一个文件夹。**装安装包的那条路用目录版**
——"一堆文件"正好是安装包最擅长处理的部分。

**安装包只给工作台做**（`tools/build_installer.py`）。`remote-agent.exe` 保持单文件：
它的卖点是"一个文件丢过去双击就跑，目标机器什么都不用装"，加一步安装等于把这个
优点换掉。

安装包解决的是**配置文件到处乱放**这个问题。单文件 exe 被拖到桌面再双击，它找不到
配置会**在 exe 旁边新建一份**（`gui/paths.py` 的查找顺序），里面是一把全新密钥；
而 MCP / CLI 还读着原来那份——两边静默分家，现象是"CC 忽然说 token 无效"。
装了安装包之后：程序在一个固定目录里，配置就是它旁边的 `config.toml`，
桌面和开始菜单给的是**快捷方式**而不是 exe 的副本；向导里勾上"让命令行客户端和
MCP 也读这份配置"，还会写一个用户环境变量 `REMOTE_PC_CONFIG` 指过去，
于是界面 / CLI / MCP 三方读的是同一个文件，"配置到底在哪"没有第二个答案。

装到哪儿由安装向导里选：默认 `%LOCALAPPDATA%\Programs\远程电脑助手`（当前用户安装，
不要管理员权限），装 D 盘也行。**但那个目录必须可写**——密钥就存在它旁边，
界面上的「随机生成密钥」要能改它；选到 `Program Files` 这类系统目录时安装向导会拦下来
并说明原因。卸载会带走程序文件，**故意留下 `config.toml` 和 `logs\`**：
重装回来还是同一把密钥，对方手里的连接串不用换。

装完想手工初始化（或者从源码跑的时候想生成一份配置）：

```bash
remote-pc.exe --init-config                      # 在程序旁边写一份 config.toml 就退出
remote-pc.exe --init-config --config D:\my.toml  # 指定路径
```

已经有配置文件就**原样不动**，不覆盖、不备份、不"顺手换成新的"。

**`remote-pc.exe` 不带黑框（`--windowed`）**：双击下去直接是工作台，不该再弹一个
控制台。`remote-agent.exe` 仍然是 `--console` —— 黑框在对方那台机器上不是装饰，
它是随时看得见每条命令的记录，"关掉黑框 = 断开"也是约定的一部分。

不带黑框要付代价：**启动阶段就崩的话，屏幕上什么都不显示**。所以另外补了三层
（都在代码里，改的时候别把它们删了）：

| 补救 | 在哪 | 管什么 |
| --- | --- | --- |
| 把标准流补上 | `gui/app.py::ensure_streams()` | 双击启动时 fd 1/2 无效，`sys.stdout/stderr` 是 `None`。`print()` 自己会安静返回，但 `logging.StreamHandler()`、uvicorn、agent 里的 `.write()` 会直接 `AttributeError`——而且是在进程还没走到 argparse 的时候就炸。这条同时负责**给命令行那条路开黑框**：`remote-pc.exe --server … --token …` 双击不起来，它是从命令行跑的，而它要的正是黑框，所以自己 `AllocConsole()` 开一个。 |
| 兜住异常 | `gui/app.py::run_gui()` | 异常不再无声无息地冒到进程外面去。 |
| 说出来 | `gui/app.py::_show_fatal()` | 先写 `logs/crash.log`（弹窗会被点掉，日志不会），再弹一个框告诉用户日志在哪。**用的是 user32 原生框**：崩在 `import PySide6` 那一步时 Qt 根本还没起来。 |

有标准流的时候一律不动，所以 `remote-pc.exe --help > out.txt` 照旧拿得到输出。

体积（PyInstaller 6.20，实测）：`remote-agent.exe` 约 14 MB；`remote-pc.exe` **70 MB**
（单文件）/ **163 MB**（`--onedir`）。差得这么多是因为它带着 PySide6 +
FastAPI + uvicorn + httpx + websockets——服务端角色要在同一个进程里跑 hub，
客户端要开窗口。agent 那份只跑命令行，一点 Qt 都不带。

> 2026-10-20 删掉内置终端之后 `winpty` 不再进包，这两个数字没重新量，下次重打
> 会再小一点。

**这个数字是瘦过身的，别让它涨回去。** 早先 `remote-pc.exe` 是 283 MB、目录版
733 MB，多出来的 400 多 MB 全是白带的：`--collect-data PySide6` 会把 site-packages 里
整个 634 MB 铺出来（其中 `Qt6WebEngineCore.dll` 195 MB 是 Chromium 内核、
`resources\*.pak` 102 MB 也是 WebEngine 的、`translations` 60 MB、`qml` 30 MB），
而这个界面只用 `QtCore / QtGui / QtWidgets`。现在改成让 PyInstaller 的
`hook-PySide6.Qt*` 按 `import` 自己判断，另外在 `EXCLUDES` 里剔掉了
numpy / PIL / lxml / Cython（约 49 MB，同样一行都没被我们 import 过）。
改 `tools/build_app.py` 的收集参数前先看文件头那段理由。

`build_app.py` 打完会自己跑三条不开窗口的命令把关：

1. `--help`（命令行那条路还在，**窗口版照样能往管道里写**）
2. `--role boss`（图形界面那套参数解析在，而且**参数错时不弹窗口**）
3. **无句柄启动**（`close_fds` + `DETACHED_PROCESS`，等价于在资源管理器里双击）
   ——这条钉的就是上面第一层补救：少了 `ensure_streams()`，进程会在
   argparse 之前就死掉。

三条都过才会印最后那段提示。

---

## 出问题先看这里

**`连不上 hub`** — 我这边 `python -m server.hub` 起了吗？对方填的 IP 对吗？
防火墙规则加了吗？先在他电脑上 `ping <我的IP>` 试试。

**`403` / `连接被拒绝`** — agent 会自己解释原因。多半是 hub 没起，
或者地址写成了 `http://` 而不是 `ws://`。

> 注意 `403` 有**两种**成因，而且长得一模一样：地址写错，或者
> **本机地址不在服务器的 `allowed_networks` 里**。后者是因为 hub 在
> `accept()` **之前**就用 4403 关了连接，那个关闭码根本传不到 agent 这边，
> 到对面只剩一个光秃秃的 HTTP 403。分不清就在**服务器端界面点「检测」**。

**`密钥不对` / 4401** — 两边 token 不一致。重跑 `python tools/gen_token.py --force`，
然后**两边都重启**（agent 会说完这句就退出，不再重试）。

> 如果 401/4401 是**忽然**出现的、而你什么都没动过，先看一眼密钥是不是被换过
> （服务器端界面上点过「随机生成密钥」，或者手动改过 `config.toml`）——
> 那种情况下**命令行 / MCP 那边不用重启**：它们收到 401 会自己重读一次配置
> 再试，长命的进程（比如一直开着的 Claude Code）也能自己好。
> 真需要动手的只有 agent：它揣着旧密钥重连会被拒。

**网关 / 状态码 4403** — 地址不在 `allowed_networks` 里。这一条 agent 那边
通常表现为 403（同上）；服务器端界面会黄字提醒你，把地址加进去或者换块网卡。

**`被顶掉了` / 4402** — 另一处用**同一个机器名**连上了（agent 默认用
`socket.gethostname()`）。这一条 agent 也会直接退出：继续重连的话两条连接会
一直互相踢，永远停不下来。如果是你自己刚开的，关掉那个旧的就行。

**中文输出乱码** — 正常不该出现。agent 是这么处理的：先按 UTF-8 严格解码，
碰到**不是合法 UTF-8 的字节**就永久切到 GBK（中文 Windows 的控制台代码页），
之后一直用 GBK。所以 `ipconfig` 这类老程序的中文能看，PowerShell 的 UTF-8 中文也能看。

只有一种情况会失手：一条命令的输出里**先出现 UTF-8 中文、后面又出现 GBK 中文**
（比如 `Write-Output "中文"; ipconfig`）。这时后面的会乱。
真遇到了就把命令拆成两条跑，或者告诉我，我再想辙。

**命令超时了但没停** — 那是设计如此，超时只是**返回**。
用 `python -m server.cli kill <session>` 停掉，或者 `output` 看进展。

**弹窗没出来 / 命令一直卡着** — 对方屏幕上是不是有别的全屏窗口盖住了？
弹窗是置顶的（`MB_TOPMOST`），但全屏游戏之类可能仍然盖住。

**图形窗口没出来 / 一闪就没了** — 默认那版**没有黑框**，所以不会"报错在黑框里"。
先看 `logs\crash.log`（就在 exe 旁边那个 `logs` 目录，写不了才退到
`%LOCALAPPDATA%\remote-pc\logs`），再不行重新打一份带黑框的：
`python tools/build_app.py --console`，启动阶段每一步都看得见。
这三种情况最像：PySide6 没打进去、端口被占、配置读不动。

**选了「服务器端」，窗口说读配置失败** — 窗口里会写出用的是哪个文件、错误原文。
多半是 TOML 语法写坏了，或者 `mode` 写成了不认识的值。
（配置坏不影响**客户端**：客户端一个字节都不读服务端的配置。）

**客户端连不上，但看不出为什么** — 点**「检测」**。它不连接、只探一次，
然后逐项说出断在哪一步：

```
✓ TCP 8765 可达
✓ 密钥正确（/api/status 返回 200）
✗ 本机地址 100.64.3.7 不在服务器允许的网段内
   → hub 会在握手前就用 4403 拒掉这条连接。把 100.64.3.7 加进
     服务器端的 allowed_networks（现在是 192.168.0.0/16），或者改用同一网段的网卡
在线 1 台  会话码 045387
```

能区分开的几种情况：端口没人听 / 密钥不对 / 服务器端压根没配 token /
那个端口上跑的是别的服务 / 本机地址不在 `allowed_networks` 内（`100.64.x.x`
这种 CGNAT、Tailscale 很常见）。**一条条列出来，而不是笼统的"连不上"。**

界面上的地址和密钥是从上次成功连接记下来的，**那不代表现在还能用**——
日志里会写明这是历史记录，先点「检测」验一次。

**服务器端启动了服务，对方还是连不上** — 启动之后界面会自己跑一次
**本机自检**（从 `127.0.0.1` 打 `/api/status`），结果写进活动日志：

- 自检**通过** → hub 这边没问题，问题在网络/网段那头（让对方点「检测」）
- 自检**没过** → 上面标 ✗ 的那一步就是原因，跟对方无关

另外，服务器端选的这个本机地址如果在 `allowed_networks` 之外，
「连接信息」下面会直接黄字提醒——不用等对方连不上才发现。

**两台 agent 同时在线** — 服务器端窗口会逼你从下拉框里选一台；
不选的话每个接口都会拒绝（hub 自己也不猜）。

**截屏是全部显示器，不是单块屏** — agent 用的是 VirtualScreen，
分辨率是"所有显示器拼起来"的画面。按钮上照实写着「全屏（含所有显示器）」。

**改了密钥，MCP / CLI 怎么还能用？** — 因为读的是**同一个** `config.toml`
（见「配置到底读哪个文件」）。改完在线的 agent 要**关掉重开**——
不是"等一下它自己重连"：密钥不对时 agent 会说明原因后直接退出，
不再每 30 秒刷一遍同一句报错。

---

## 这个工具**不会**做的事

这些不是"还没做"，是**明确不做**：

- **不隐藏进程** —— agent 就是一个普通的可见窗口，任务管理器里一眼看见
- **不随系统启动** —— 没有自启注册表项、没有启动文件夹快捷方式、没有计划任务
- **不装成服务** —— 不常驻、不静默运行
- **不提供静默模式** —— 没有一个"不问就执行"的开关可以绕过弹窗
- **不自我更新** —— 对方的 exe 永远不会自己换掉自己
- **没有托盘、没有最小化到后台** —— 图形窗口就是显示器，黑框才是记录，
  两个都关不掉的"后台运行"在这里不存在
- **确认弹窗没有替代品** —— 它是系统置顶的 `MessageBoxTimeoutW`，
  不是窗口内的对话框，也没有"本次会话全部同意"这类开关

它是一个**双方都看得见、随时能拔掉**的远程协助工具。任何让它变得
"对方看不见"的改动都不该被接受。

---

## 开发

打包：`python tools/build_app.py` → `dist/remote-pc.exe`，
`python tools/build_agent.py` → `dist/remote-agent.exe`（都要 `pip install pyinstaller`）。

```
app.py            入口（PyInstaller 打的就是这个）
server/
  protocol.py     线上消息格式，两端共用（含 4401/4402/4403 关闭码）
  policy.py       危险命令判定（agent 侧有一份手抄副本，改动要两边同步）
  registry.py     agent 连接、会话、输出环形缓冲
  hub.py          FastAPI：agent 入口 + 本地 HTTP API
  remote.py       CLI/MCP/界面用的 HTTP 客户端
  mcp_server.py   FastMCP，11 个工具
  cli.py          命令行调试工具
agent/
  agent.py        单文件、零第三方依赖——对方拿到就能跑
gui/
  app.py          横幅、控制台标题、标准流补齐（没有黑框时）、命令行直通
  pyside_app.py   PySide6 工作台：欢迎页 / 服务器端页 / 客户端页
  chat.py         消息的校验和排版
  hubctl.py       把 hub 起在界面自己的进程里
  paths.py        config / 日志目录到底在哪
  conninfo.py     连接串编解码
  store.py        记住上次连过的地址
  netinfo.py      本机局域网 IPv4 + 出口地址探测（source_ip_for）
  linkcheck.py    连接前自检：逐项说清"哪一步断了"
  toml_edit.py    只改 token 那一行的写入器（原子写 + .bak）
tools/
  gen_token.py    生成密钥、探测局域网 IP
  build_app.py    打包成 remote-pc.exe（两个角色；默认不带黑框，--console 可改回）
  build_agent.py  打包成 remote-agent.exe（只有客户端，单文件）
  build_installer.py  打安装包（要 Inno Setup；先有 dist/remote-pc/ 目录版）
  installer/
    remote-pc.iss   Inno Setup 脚本（纯文本，能 diff）
```

### 改完之后怎么验

```bash
python app.py                  # 起工作台，手动走一遍：
```

- **服务器端** → 点「随机生成密钥」→ 确认日志里写出 `config.toml` 的路径
  **和它是怎么被找到的**，`config.toml.bak` 真的生成，文件里 `token` 那行确实换了，
  而注释和 `allowed_networks` 原样保留（拿真文件 diff 看一眼）。
- **服务器端** → 点「启动服务」→ 活动日志里出现本机自检的结果。
- **客户端** → 故意填错密钥 → 点「检测」→ 必须明确说**密钥不对**，
  而不是笼统的连不上。
- **客户端** → 填一个没人监听的端口 → 「端口不通」。
- **客户端** → 填对 → 「在线 N 台  会话码 xxxxxx」。
- **客户端** → 把地址换成本机不在 `allowed_networks` 里的那种 → 第 ③ 项必须报红。
- **agent 侧** → `python agent/agent.py --server ws://… --token 故意写错`
  → 黑框显示「密钥不对…」并**退出**，不再无限刷屏。
- **换密钥那一套** → hub 跑着的时候点「随机生成密钥」→ 先弹一个框说清代价；
  点「算了」就什么都不动（界面、文件、hub 三边都原样）；点「继续」之后
  `config.toml` 立刻是新密钥，而且**不重启 hub 新连接就能连上**。
  另开一个终端 `python -m server.cli status` 应该立刻就好——它会自己重读配置。
- **弹黑框** → 用 `--role client` 连上之后执行一条命令，对方屏幕上**不该**
  出现任何新窗口。

打包之后（`python tools/build_app.py` → `dist/remote-pc.exe`）要重跑一遍
"生成密钥 + 写回"那两条：冻结版走的是另一条路径解析代码
（`paths.resolve_config_path` 会往上找 3 层），得确认它写回的仍是仓库根那份。

**`build_app.py` 自己跑的那三条冒烟挡不住最重要的一种坏法。** 它们验的是命令行和
参数解析（`--help` / 参数错 / 无句柄启动），**一个都不 import Qt**——所以哪怕 Qt 平台
插件被漏收，三条也照样全绿，坏到用户那儿是"双击没反应"。同理，`EXCLUDES` 里多写一个
包名，源码测试一条都不会红。这两样只有把冻结版真跑起来才发现：

```bash
# 1. 窗口真的开出来了（只有这个能证明 qwindows.dll 等平台插件在包里）
dist\remote-pc\remote-pc.exe        # 双击，能看到「远程电脑助手」窗口

# 2. 端到端：源版 hub + **冻结版** agent，连上并执行一条命令
python -m server.hub
dist\remote-pc\remote-pc.exe --server ws://127.0.0.1:8765 --token <密钥> --mode auto
python -m server.cli status         # 应该看到 1 台在线
python -m server.cli exec "echo ok" # 输出里应该有 ok
```

> 单文件版（默认那版）是 **bootloader 先起、再 fork 出一个子进程跑真正的程序**。
> 所以按窗口句柄去找进程时，**窗口属于子进程，PID 跟 `Popen` 拿到的那个不一样**；
> 按父 PID `taskkill` 也会留下子进程继续跑。验收用无头脚本做的话，认**窗口标题**
> 或者 `/T` 连子进程一起杀。

安装包那条路另外验两条：

```bash
python tools/build_app.py --onedir
dist\remote-pc\remote-pc.exe --init-config        # 写完 config.toml 就退出，不开窗口
dist\remote-pc\remote-pc.exe --init-config        # 再跑一次：应该原样不动
python tools/build_installer.py
```

**`.iss` 编得过 ≠ 装得上**，而且它有三条只有真装一遍才验得到的承诺。静默装到
一个临时目录就能全过一遍（`/TASKS` 不给，所以不会写 `REMOTE_PC_CONFIG`、
也不会建桌面图标）：

```bash
dist\远程电脑助手-安装程序.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /DIR=D:\tmp\试装
```

1. 装完要有 `remote-pc.exe` + `_internal\` + `unins000.exe`；
2. **[Run] 那一步真的播种了配置**：`D:\tmp\试装\config.toml` 存在，里面是
   `[server]` + 一把 43 字符的新密钥；
3. **再装一次不覆盖它**：先在文件末尾手加一行注释，重装一遍，那行得还在
   （`--init-config` 发现文件已存在就什么都不做，这条靠的就是它）；
4. 跑 `unins000.exe /VERYSILENT` **卸载**：程序文件清掉，`config.toml` **留下**。

装完之后人工确认：开始菜单/桌面是**快捷方式**而不是 exe 的副本；勾了
"让命令行客户端和 MCP 也读这份配置"的话，`echo %REMOTE_PC_CONFIG%` 指向那个文件。

`agent/agent.py` 改过就要重新出 `dist/remote-agent.exe` 发给对方
（这次改了：子进程不再弹黑框）。

### 真开窗口的验收

这一版没有自动化的窗口验收，按「改完之后怎么验」那张清单用 `python app.py`
手动走一遍。

改动 `agent/agent.py` 的断开逻辑时额外确认三件事：点「断开」后 hub **一秒内**
看到 agent 离线、断开前新起的 powershell / ping.exe **一个都不剩**、审计文件
多一行 `agent_disconnect`。这三条错了就是「偷偷在对方电脑上留了个进程」。

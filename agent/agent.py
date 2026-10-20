#!/usr/bin/env python3
"""远程终端 agent —— 跑在**被操作**的那台电脑上。

设计原则（如果你是被操作的一方，下面这些就是你该关心的事）：
  * 单文件、零依赖（除了 websockets），从头到尾可以读完
  * 主动连出，本机不开任何监听端口，不改防火墙，不写自启动，不装服务
  * 每条命令都在这个窗口里打印出来，你随时能看见它在干什么
  * 危险命令会弹框问你，默认按钮是"否"，不点就是拒绝；超时也算拒绝
  * 按 Ctrl+C 立即退出，对方立刻失去控制

用法：
    python agent.py --server ws://10.22.114.127:8765 --token <密钥>
    python agent.py --config agent.toml

依赖：pip install websockets
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import ctypes
import hashlib
import hmac
import json
import os
import random
import re
import signal
import socket
import subprocess
import sys
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path

try:
    from websockets.asyncio.client import connect
except ImportError:  # 老版本 websockets 的导入路径
    try:
        from websockets import connect  # type: ignore
    except ImportError:
        sys.exit("缺少依赖，请先运行：pip install websockets")

PROTO_VERSION = 1

# --- 协议常量（与 server/protocol.py 一一对应，改一边必须同步另一边） ---
T_CHALLENGE, T_READY, T_EXEC, T_STDIN, T_SIGNAL = "challenge", "ready", "exec", "stdin", "signal"
T_PUT_BEGIN, T_PUT, T_PUT_END, T_GET = "put_begin", "put", "put_end", "get"
T_SHOT, T_CLIP_GET, T_CLIP_SET, T_PING = "shot", "clip_get", "clip_set", "ping"
T_AUTH, T_CHUNK, T_DONE, T_DENIED = "auth", "chunk", "done", "denied"
T_FILE_BEGIN, T_FILE_CHUNK, T_FILE_DONE = "file_begin", "file_chunk", "file_done"
T_SHOT_DATA, T_CLIP_DATA, T_ERROR, T_PONG = "shot_data", "clip_data", "error", "pong"
T_CHAT_SERVER = "chat_server"   # 服务器端发来的消息
T_CHAT = "chat"                 # 本机发出去的消息

UI_GUI, UI_CONSOLE = "gui", "console"

# WebSocket 关闭码（4000-4999 是应用保留段）。和 server/protocol.py 里那三个一致，
# test_protocol.py 会断言这一点——抄漏一个，界面上就少一种能说清的失败原因。
CLOSE_BAD_AUTH = 4401          # 密钥不对
CLOSE_SUPERSEDED = 4402        # 同一个机器名的 agent 又连了一条，旧的被顶掉
CLOSE_NETWORK_DENIED = 4403    # 对方地址不在 allowed_networks 内

FILE_CHUNK_SIZE = 256 * 1024
MAX_TRANSFER_BYTES = 512 * 1024 * 1024

# 一条消息的字符上限，和 server/protocol.py 一致。
MAX_CHAT_CHARS = 2000


def normalize_chat_text(text) -> str | None:
    """消息正文的校验：必须是非空字符串且不超长，否则返回 None。"""
    if not isinstance(text, str):
        return None
    text = text.strip()
    if not text or len(text) > MAX_CHAT_CHARS:
        return None
    return text

# 这些消息的处理可能耗时很久，必须放到后台任务里跑，
# 否则会把接收循环堵住。上传分片（put_begin/put/put_end）不在其中——
# 它们要保持严格按序，所以留着同步处理。
_SLOW_OPS = frozenset({T_EXEC, T_GET, T_SHOT, T_CLIP_GET, T_CLIP_SET})

# 命令结束哨兵。命令跑完后我们把当前工作目录和退出码夹带回来，
# 这样 `cd` 才能跨命令生效。
SENTINEL = "__RC__"
SENTINEL_RE = re.compile(SENTINEL + r"(-?\d*)\|([^\r\n]*)")
HOLD_BACK = 1024  # 尾巴上保留这么多字符不急着发，保证哨兵行没被切碎

# 点了"断开"之后，等这么久还没退出来才动硬的（取消任务）。
# 正常的路径是：标志位 → 关 WebSocket → run() 自然返回 → finally 里干净地收尾。
# 取消只是看门狗，不是主机制——见 Agent.stop() 里的说明。
STOP_GRACE_S = 5.0

# ctrl-c 发完 CTRL_BREAK 后等这么久，还没退就强杀。
# 控制事件在"进程没挂控制台"或"卡在不响应控制事件的调用里"时会静默失效。
CTRL_BREAK_GRACE_S = 3.0


def _has_console() -> bool:
    """本进程背后有没有控制台。

    `remote-agent.exe` 是 `--console` 打的、双击就有；`remote-pc.exe` 是
    `--windowed` 打的，客户端页面那种模式下**没有**（`gui/app.py` 只在命令行
    agent 那条路上 AllocConsole）。取不到就当没有——保守的那一边。
    """
    if os.name != "nt":
        return False
    try:
        return bool(ctypes.windll.kernel32.GetConsoleWindow())
    except Exception:  # noqa: BLE001 - 拿不到就算了，按"没有控制台"处理
        return False


def child_creationflags(has_console: bool | None = None, *,
                        nt: bool | None = None) -> int:
    """起子进程时该带的 Windows 创建标志。纯计算，测试直接喂参数进来。

    两件事：

    * `CREATE_NEW_PROCESS_GROUP` —— 独立进程组。这样对方按 Ctrl+C 不会连带
      杀掉远程命令，也让我们能用 CTRL_BREAK 精准中断远程命令（`_on_signal`）。
    * **自己没控制台时再加 `CREATE_NO_WINDOW`。** Windows 的规矩是：父进程
      没有控制台、而要起的又是控制台程序，系统会给它**新分配一个控制台窗口**。
      于是每执行一条远程命令，对方屏幕上就闪一个黑框——GUI 客户端模式下
      （agent 在 PySide 进程里跑，`--windowed` 没控制台）正是这样。截屏和读写
      剪贴板走的是同一个坑，每截一次屏也闪一下，等于通知对方"我在看你"。

    为什么只在"没控制台"时才加：有控制台时子进程直接继承它，本来就不弹窗口，
    而且那种情况下 `os.kill(pid, CTRL_BREAK_EVENT)` 是能送到的。反过来，**没有**
    控制台时那个调用本来就送不到（`GenerateConsoleCtrlEvent` 要求调用方挂着
    控制台），代码走的是 `_on_signal` 里"发不出去，直接强杀"那一支。所以在
    这种情况下加 `CREATE_NO_WINDOW` 是零代价：去掉的只是一个已经废掉的窗口。

    被隐藏的**不是**"对方看得见的那份记录"——那份是 `say()` 打到 agent 自己
    控制台上的 `$ 命令原文`，加上 audit 日志。这里藏的只是每条命令临时的子窗口，
    它一闪而过，谁都看不清。
    """
    if nt is None:
        nt = os.name == "nt"
    if not nt:
        return 0
    if has_console is None:
        has_console = _has_console()
    flags = subprocess.CREATE_NEW_PROCESS_GROUP
    if not has_console:
        flags |= subprocess.CREATE_NO_WINDOW
    return flags


# ===========================================================================
# 命令策略（server/policy.py 的手抄副本，两边规则必须一致）
# ===========================================================================
ALLOW, CONFIRM, DENY = "allow", "confirm", "deny"
MODE_SAFE, MODE_CONFIRM, MODE_AUTO = "safe", "confirm", "auto"
MODES = (MODE_SAFE, MODE_CONFIRM, MODE_AUTO)

DENY_PATTERNS: list[tuple[str, str]] = [
    (r"\bformat\s+[a-z]:", "格式化磁盘"),
    (r"\bdiskpart\b", "磁盘分区工具"),
    (r"\bbcdedit\b", "修改启动配置"),
    (r"\bvssadmin\s+delete\s+shadows\b", "删除卷影副本（勒索软件常见手法）"),
    (r"\bcipher\s+/w\b", "擦除磁盘可用空间"),
    (r"\bwevtutil\s+cl\b", "清空事件日志"),
    (r"\bnet\s+user\s+\S+(?:\s+\S+)?\s+/add\b", "新建系统账户"),
    (r"\bnet\s+localgroup\s+administrators\s+\S+\s+/add\b", "将账户提权为管理员"),
    (r"\bnetsh\s+advfirewall\s+set\b.*\boff\b", "关闭防火墙"),
    (r"\breg\s+delete\s+hklm\b", "删除 HKEY_LOCAL_MACHINE 注册表项"),
    (r"\bset-mppreference\b.*disablerealtimemonitoring", "关闭 Defender 实时防护"),
    (r"\brm\s+-rf\s+/(?!\w)", "删除根目录"),
    (r"\b(?:rd|rmdir)\s+/s\s+/q\s+[a-z]:\\?\s*$", "递归删除整个盘符"),
    (r"\bdel\s+/[fsq].*?\b[a-z]:\\\*\s*$", "递归删除整个盘符内容"),
    (r"remove-item\b(?=.*-recurse)(?=.*-force).*?\b[a-z]:\\?\s*(\||;|$)", "递归强制删除盘符根目录"),
    (r"\b(?:rd|rmdir)\s+/s\s+/q\s+\"?[a-z]:\\(?:windows|program files)",
     "递归删除系统目录"),
    (r"remove-item\b(?=.*-recurse)(?=.*-force).*?\\(?:windows|program files)\b",
     "递归强制删除系统目录"),
]

CONFIRM_PATTERNS: list[tuple[str, str]] = [
    (r"\bshutdown\b|\bstop-computer\b", "关机"),
    (r"\brestart-computer\b|\breboot\b", "重启"),
    # 注意别写成 \b/f\b：斜杠和连字符是非单词字符，\b 在这里永远匹配不上
    (r"\btaskkill\b.*(?:^|\s)/f\b", "强制结束进程"),
    (r"\bstop-process\b.*(?:^|\s)-force\b", "强制结束进程"),
    (r"\b(?:reg\s+add|new-itemproperty)\b.*\\run\b", "写入开机自启注册表项"),
    (r"\bschtasks\s+/create\b", "创建计划任务"),
    (r"\bnew-service\b|\bsc\s+create\b", "创建系统服务"),
    (r"\b(?:rm|del|rd|rmdir|remove-item)\b", "删除操作"),
    (r"\b(?:move|mv|move-item|rename|ren|rename-item)\b", "移动/重命名"),
    (r"\b(?:icacls|takeown|attrib)\b", "修改权限或属性"),
    (r"\bset-executionpolicy\b", "修改 PowerShell 执行策略"),
    (r"\b(?:net\s+user\s+\S+\s+/delete|net\s+user\s+\S+\s+\*)", "修改账户密码"),
    (r"\b(?:wmic|set-ciminstance)\b.*(?:^|\s)[-/]?delete\b", "WMI 删除操作"),
    (r"\bbcdedit\b|\breg\s+(?:add|import)\b", "修改启动项/注册表"),
    (r"\b(?:git\s+push|git\s+reset\s+--hard|git\s+clean\s+-[a-z]*f)", "可能导致代码丢失的 git 操作"),
    (r"\b(?:pip|npm|pnpm|yarn|winget|choco)\s+(?:install|uninstall|add|remove)\b",
     "安装/卸载软件"),
    (r"\bcurl\b.*\|\s*(?:ba)?sh", "管道执行远程脚本"),
]

SAFE_PATTERNS: list[str] = [
    r"dir\b", r"ls\b", r"type\b", r"cat\b", r"tree\b", r"where\b",
    r"get-childitem\b", r"gci\b", r"get-content\b", r"get-item\b",
    r"get-itemproperty\b", r"get-location\b", r"get-command\b",
    r"get-process\b", r"get-service\b", r"get-volume\b",
    r"get-ciminstance\b", r"get-netip\w*\b", r"get-computerinfo\b",
    r"select-string\b", r"findstr\b", r"sort\b", r"measure-object\b",
    r"cd\b", r"pwd\b", r"echo\b", r"hostname\b", r"whoami\b", r"ver\b",
    r"ipconfig\b", r"systeminfo\b", r"tasklist\b", r"netstat\b",
    r"ping\b", r"tracert\b", r"nslookup\b", r"arp\b", r"route\s+print\b",
    r"wmic\b", r"python\s+--version\b", r"node\s+--version\b",
    r"git\s+(?:status|log|diff|show|branch|remote)\b",
    r"test-path\b", r"get-help\b", r"cls\b", r"clear-host\b",
    # 纯输出/等待类，副作用为零
    r"write-output\b", r"write-host\b", r"write-error\b", r"write-warning\b",
    r"out-null\b", r"out-string\b", r"select-object\b", r"sort-object\b",
    r"foreach-object\b", r"where-object\b", r"format-table\b",
    r"convertto-json\b", r"convertfrom-json\b",
    r"\[console\]::", r"start-sleep\b", r"exit\b",
]


def _segments(cmd: str) -> list[str]:
    """把命令按 ; | && || 换行 拆成片段，并剥掉常见的包装前缀。

    拆开只会让 safe 模式的判定**更严**（任一片段认不出来就拒），
    所以即使因为引号里的符号拆错了，方向也是安全的。
    """
    out: list[str] = []
    for part in re.split(r"(?:&&|\|\||[;\n|])+", cmd):
        part = part.strip()
        if not part:
            continue
        part = re.sub(r"^\$[\w:]+\s*=\s*", "", part).strip()  # $x = ... 赋值前缀
        part = part.lstrip("(&{").strip()
        if part:
            out.append(part)
    return out


def _compile(patterns):
    return [(re.compile(p, re.IGNORECASE), label, p) for p, label in patterns]


_DENY, _CONFIRM = _compile(DENY_PATTERNS), _compile(CONFIRM_PATTERNS)
_SAFE = [re.compile(p, re.IGNORECASE) for p in SAFE_PATTERNS]


def check_command(cmd: str, mode: str) -> tuple[str, str, str]:
    """返回 (action, 原因, 命中的正则)。"""
    cmd = (cmd or "").strip()
    if not cmd:
        return DENY, "空命令", ""

    for rx, label, raw in _DENY:
        if rx.search(cmd):
            return DENY, label, raw

    if mode == MODE_SAFE:
        # 每一段都必须是认识的只读命令，整条才算安全
        segs = _segments(cmd)
        if segs and all(any(rx.match(s) for rx in _SAFE) for s in segs):
            return ALLOW, "", ""
        for rx, label, raw in _CONFIRM:
            if rx.search(cmd):
                return DENY, f"safe 模式只允许只读命令（{label}）", raw
        return DENY, "safe 模式只允许只读命令", ""

    for rx, label, raw in _CONFIRM:
        if rx.search(cmd):
            if mode == MODE_AUTO:
                return ALLOW, f"[auto 模式放行] {label}", raw
            return CONFIRM, label, raw

    return ALLOW, "", ""


def check_path(path: str, allow_root: str | None) -> tuple[bool, str]:
    """路径牢笼：解析真实路径后必须落在 allow_root 之内（防 `..` 和符号链接逃逸）。"""
    if not allow_root:
        return True, ""
    if not path:
        return False, "空路径"
    try:
        root = os.path.realpath(os.path.abspath(allow_root))
        target = os.path.realpath(os.path.abspath(path))
    except OSError as exc:
        return False, f"路径解析失败：{exc}"

    if target == root:
        return True, ""
    if os.name == "nt":
        if target.lower().startswith(root.lower().rstrip(os.sep) + os.sep):
            return True, ""
    elif target.startswith(root.rstrip(os.sep) + os.sep):
        return True, ""
    return False, f"路径越界：{target} 不在 {root} 之内"


# ===========================================================================
# 输出解码
# ===========================================================================
class StickyDecoder:
    """先按 UTF-8 解，遇到非 UTF-8 就整条流切换成 GBK。

    中文 Windows 上 PowerShell cmdlet 输出 UTF-8（我们在包装脚本里强制了），
    但 ipconfig / systeminfo 这类老的原生 exe 仍按控制台代码页吐 GBK。
    两种混在一起是常态，所以要能自动判断。

    改判是"粘"的：一旦切到 cp936 就不再回头。已经发出去的块不会被重新解码，
    所以**跨块**混排（前一块 UTF-8 中文、后一块 GBK 中文）是安全的。

    但**同一块内部**混排会连累前面那截——整块会按 cp936 重解一遍，
    于是块首的 UTF-8 中文跟着变成乱码。要避免就把两类输出分开成两条命令跑。
    feed() 是按每次 read 分块的，所以这个窗口实际很小，但确实存在。
    """

    def __init__(self) -> None:
        self.enc = "utf-8"
        self._pending = b""

    def feed(self, data: bytes) -> str:
        buf = self._pending + data
        self._pending = b""
        for _ in range(3):
            try:
                return buf.decode(self.enc)
            except UnicodeDecodeError as exc:
                if exc.reason == "unexpected end of data" and exc.end == len(buf):
                    self._pending = buf       # 多字节字符被分片截断了，等下一片
                    return ""
                if self.enc == "utf-8":
                    self.enc = "cp936"
                    continue
                return buf.decode(self.enc, errors="replace")
        return buf.decode("utf-8", errors="replace")

    def flush(self) -> str:
        if not self._pending:
            return ""
        out = self._pending.decode(self.enc, errors="replace")
        self._pending = b""
        return out


# ===========================================================================
# 确认弹窗
# ===========================================================================
def ask_confirm(cmd: str, reason: str, timeout_s: int = 60) -> bool:
    """弹系统对话框问要不要执行。默认按钮是"否"，超时也当拒绝。"""
    MB_YESNO, MB_ICONWARNING = 0x04, 0x30
    MB_TOPMOST, MB_SETFOREGROUND, MB_DEFBUTTON2 = 0x40000, 0x10000, 0x100
    IDYES = 6

    shown = cmd if len(cmd) <= 800 else cmd[:800] + "…（已截断）"
    text = (f"来自局域网的另一台电脑，想执行下面的命令：\n\n"
            f"{shown}\n\n"
            f"原因：{reason}\n\n"
            f"点击「是」后才会执行；{timeout_s} 秒内无响应视为拒绝。")
    flags = MB_YESNO | MB_ICONWARNING | MB_TOPMOST | MB_SETFOREGROUND | MB_DEFBUTTON2

    try:
        user32 = ctypes.windll.user32
    except AttributeError:
        return False

    try:
        res = user32.MessageBoxTimeoutW(None, text, "远程指令确认", flags, 0, timeout_s * 1000)
    except AttributeError:
        # 老系统没有带超时的版本，只能让对话框一直等着
        res = user32.MessageBoxW(None, text, "远程指令确认", flags)
    return res == IDYES


# ===========================================================================
# 审计日志
# ===========================================================================
def default_audit_path() -> Path:
    """审计日志放哪。

    打包成 exe 之后不能再用 __file__ —— 单文件模式下它指向
    PyInstaller 解压到的 %TEMP%\\_MEIxxxxxx，那个目录**退出时就删掉**，
    审计记录等于没留。所以改成放在 exe 旁边（看得见、好交付）；
    那儿不可写（比如装在 Program Files）就退到 %LOCALAPPDATA%。
    """
    if getattr(sys, "frozen", False):
        beside = Path(sys.executable).resolve().parent
        if os.access(beside, os.W_OK):
            return beside / "agent-audit.jsonl"
        return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "remote-agent" \
            / "agent-audit.jsonl"
    return Path(__file__).resolve().parent / "agent-audit.jsonl"


class AuditLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: str, **fields) -> None:
        rec = {"ts": time.time(), "event": event, **fields}
        try:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass


# ===========================================================================
# 包装脚本：把用户的命令包起来，跑完把 cwd 和退出码夹带回来
# ===========================================================================
def build_launch(cmd: str, cwd: str, shell: str) -> tuple[list[str], bytes | None]:
    """返回 (argv, 需要写进去的包装脚本 or None)。

    包装脚本一律走 argv 传（-EncodedCommand / -c），**不占用 stdin**，
    这样子进程的 stdin 就空出来了，可以喂交互式输入。
    """
    if shell in ("powershell", "pwsh"):
        exe = "powershell" if shell == "powershell" else "pwsh"
        script = (
            "$ErrorActionPreference = 'Continue'\n"
            "$ProgressPreference = 'SilentlyContinue'\n"
            "try { chcp 65001 | Out-Null } catch {}\n"
            "try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}\n"
            "try { $OutputEncoding = [Text.Encoding]::UTF8 } catch {}\n"
            f"Set-Location -LiteralPath {powershell_quote(cwd)}\n"
            f"{cmd}\n"
            "$__ec = if ($LASTEXITCODE -ne $null) { $LASTEXITCODE } elseif ($?) { 0 } else { 1 }\n"
            f'Write-Output "{SENTINEL}$__ec|$((Get-Location).Path)"\n'
        )
        # -EncodedCommand 收 base64(UTF-16LE)，绕开所有引号转义的坑
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        return [exe, "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-EncodedCommand", encoded], None

    if shell == "cmd":
        # cmd 的 /c 每次都是新进程，`cd` 本来就不会跨命令生效，
        # 所以不用哨兵：工作目录由 subprocess 的 cwd= 决定。
        return ["cmd.exe", "/Q", "/D", "/C", cmd], None

    if shell == "bash":
        script = (f'cd {shlex_quote(cwd)} || exit 1\n'
                  f'{cmd}\n'
                  f'__ec=$?\n'
                  f'echo "{SENTINEL}$__ec|$PWD"\n')
        return ["bash", "-c", script], None

    raise ValueError(f"不支持的 shell：{shell!r}")


def powershell_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def shlex_quote(s: str) -> str:
    import shlex
    return shlex.quote(s)


# ===========================================================================
# 会话
# ===========================================================================
class Session:
    def __init__(self, name: str, cwd: str, shell: str) -> None:
        self.name = name
        self.cwd = cwd
        self.shell = shell
        self.proc: asyncio.subprocess.Process | None = None
        self.cmd_id: str | None = None
        self._lock: asyncio.Lock | None = None

    @property
    def lock(self) -> asyncio.Lock:
        # 懒建：确保锁属于当前事件循环
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock


class Failure:
    """一次连接失败：给人看的文案 + 还该不该重连。

    `fatal=True` 的意思是"再连一万次也是同样的结果"——重连只会让同一句
    文案每 30 秒刷一遍，把真正该看的那一行冲走。目前只有密钥不对属于这类。

    刻意不用 @dataclass：这个类只有两个字段，手写 __init__ 更直接，
    也不用操心异常基类的 __init__ 约定。
    """

    __slots__ = ("text", "fatal")

    def __init__(self, text: str, fatal: bool = False) -> None:
        self.text = text
        self.fatal = fatal


class Agent:
    # 图形界面（gui/pyside_app.py 里的 AgentRunner）会把它换成一个回调，命令行用法下保持 None，
    # 于是一行代码都不多跑。见 emit()。
    on_event = None

    def __init__(self, cfg: dict) -> None:
        self.server_url: str = cfg["server_url"]
        self.token: str = cfg["token"]
        self.mode: str = cfg.get("mode", MODE_CONFIRM)
        self.allow_root: str | None = cfg.get("allow_root")
        self.shell: str = cfg.get("shell", "powershell")
        self.default_cwd: str = cfg.get("default_cwd") or str(Path.home())
        self.max_lifetime: float | None = cfg.get("max_lifetime")
        self.confirm_timeout: int = int(cfg.get("confirm_timeout_s", 60))
        self.name: str = cfg.get("name") or socket.gethostname()
        # 这个 agent 是图形界面的客户端页跑起来的，还是命令行/黑框版。
        # 服务器端据此决定"消息"能不能用：黑框里没有地方显示聊天。
        self.ui: str = cfg.get("ui", UI_CONSOLE)
        self.audit = AuditLog(Path(cfg.get("audit_path") or default_audit_path()))
        self.sessions: dict[str, Session] = {}
        self.clipboard_text: str = ""
        self._uploads: dict[str, dict] = {}
        self._tasks: set[asyncio.Task] = set()
        self._starting = time.time()
        # 停止相关的状态。`stop()` 从别的线程调，所以这几个字段的读写
        # 要经得起跨线程（都是简单的赋值/读取，不需要额外的锁）。
        self._stopping = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._run_task: asyncio.Task | None = None
        self._ws = None
        # 握手完成才算"能发消息"。握手前 `_ws` 就已经不是 None 了，这时候
        # 插一条消息进去会把 auth 帧挤到后面，hub 直接把连接关掉。
        self._chat_ready = False
        # `_serve` 认出来的"必须停下来"的原因。`run()` 拿它来 emit——
        # 图形界面是靠事件的 text 字段写日志的，只说给黑框听等于没说。
        self._fatal_text = ""

    # -- 界面挂点 -----------------------------------------------------------
    def emit(self, kind: str, **fields) -> None:
        """把一条结构化事件交给界面（没有界面就直接返回）。

        **回调抛出的任何异常都吞掉。** 界面是我们自己写的，它出问题绝不该
        反过来影响正在跑的命令——尤其是"危险命令正在等确认"的那一刻：
        那时候一个从回调里冒出来的异常，会把确认流程整个带偏。
        """
        handler = self.on_event
        if handler is None:
            return
        try:
            handler(kind, fields)
        except Exception:
            pass

    # -- 输出 ---------------------------------------------------------------
    def say(self, text: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        print(f"[{stamp}] {text}", flush=True)
        # 命令行行为逐字不变（上面那行就是）；下面这句只是让界面也能看到同一句话。
        # 图形窗口是**显示器**，控制台才是**记录**——两边都要有，所以不挪走 print。
        self.emit("say", text=f"[{stamp}] {text}")

    # -- 会话 ---------------------------------------------------------------
    def session(self, name: str) -> Session:
        sess = self.sessions.get(name)
        if sess is None:
            sess = Session(name, self.default_cwd, self.shell)
            self.sessions[name] = sess
        return sess

    # -- 主循环 -------------------------------------------------------------
    def _out_of_time(self) -> bool:
        if not self.max_lifetime:
            return False
        if time.time() - self._starting <= self.max_lifetime:
            return False
        self.say(f"已达最长运行时间 {self.max_lifetime} 秒，主动退出。")
        return True

    async def _serve_within_lifetime(self, ws) -> bool:
        """给 _serve 套一层寿命限制。

        必须在**连着的时候**也能到点退出：只在外层循环判断的话，
        连接一直健康、_serve 一直不返回，寿命限制就永远轮不到，
        --max-lifetime 等于没写。
        """
        if not self.max_lifetime:
            return await self._serve(ws)
        left = self.max_lifetime - (time.time() - self._starting)
        try:
            return await asyncio.wait_for(self._serve(ws), max(left, 0.1))
        except asyncio.TimeoutError:
            self.say(f"已达最长运行时间 {self.max_lifetime} 秒，主动退出。")
            return False

    async def run(self) -> None:
        backoff = 1.0
        # 记下自己跑在哪个循环、哪个任务里：stop() 要从别的线程找到它们。
        self._loop = asyncio.get_running_loop()
        self._run_task = asyncio.current_task()
        try:
            while True:
                if self._stopping or self._out_of_time():
                    return
                try:
                    async with connect(self.server_url, max_size=4 * 1024 * 1024,
                                       ping_interval=20, ping_timeout=20,
                                       open_timeout=10) as ws:
                        self._ws = ws
                        backoff = 1.0
                        try:
                            if not await self._serve_within_lifetime(ws):
                                # `_serve` 认得出来的原因（比如被同名顶掉）要一起带上，
                                # 不然界面上是"连接异常"三个字，什么都没说。
                                self.emit("fatal", text=self._fatal_text)
                                return
                        finally:
                            # 连接一结束就清掉引用，_begin_stop() 才不会对着
                            # 一个已经关掉的 socket 调 close()。
                            self._ws = None
                            self._chat_ready = False
                            self.emit("disconnected")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    failure = self._explain(exc)
                    if failure.fatal:
                        # 再连一万次也是同样的结果，而每 30 秒刷一遍同一句
                        # 反而会把"密钥不对"这条真正该看的冲走。停下。
                        self.say(f"连接断开：{failure.text}")
                        self.emit("fatal", text=failure.text)
                        return
                    self.say(f"连接断开：{failure.text}；{backoff:.0f} 秒后重连")
                    self.emit("error", text=failure.text)

                if self._stopping or self._out_of_time():
                    return
                # 指数退避 + 抖动，避免服务端刚起来时一堆 agent 同时重连
                self.emit("reconnecting", seconds=backoff)
                await asyncio.sleep(backoff + random.uniform(0, backoff * 0.3))
                backoff = min(backoff * 2, 30.0)
        finally:
            self._ws = None
            self._chat_ready = False
            self._run_task = None
            # 无论从哪条路退出（鉴权失败、到点、异常、被断开），都要把残留的子进程收掉
            await self._cleanup_children()
            if self._stopping:
                # 这两个字要落在**黑框**里：图形窗口关掉就没了，控制台才是记录。
                # 排在收尾之后说，因为这时候子进程确实已经收干净了。
                self.say("连接已关闭。")

    # -- 停止（给界面用）----------------------------------------------------
    def stop(self) -> None:
        """让 run() 尽快结束。**从别的线程调用**（界面的"断开"、关窗口）。

        幂等、绝不阻塞调用方、绝不抛异常。

        **顺序就是全部要点：先立标志位，取消只是看门狗。** 标志位让 run() 在
        WebSocket 关掉之后**正常**返回，于是 `finally` 里的 `_cleanup_children()`
        走的是"没有 CancelledError 在飞"的那条路——里面那个
        `await asyncio.to_thread(_kill_tree, pid)` 能跑完，每个子进程都吃到
        `taskkill /F /T`。反过来"先取消"的写法里，同一个 await 是在取消过程中跑的：
        CPython 确实会执行它，但离"被第二次取消、于是拿不到子进程"只差一步
        （再点一次断开，或者 asyncio.run 收尾）。干净路径一定杀干净、
        边缘路径可能杀不干净——这条不对称就足以选标志位优先。
        """
        if self._stopping:
            return
        self._stopping = True
        loop = self._loop
        if loop is None or loop.is_closed():
            return                      # run() 还没起 / 已经结束：标志位已经够了
        try:
            loop.call_soon_threadsafe(self._begin_stop)
        except RuntimeError:
            pass                        # 循环正在关闭，标志位也已经立好了

    def _begin_stop(self) -> None:
        """在 agent 自己的事件循环里执行。"""
        if self._ws is not None:
            # 走的是正规的 close 帧：hub 那边会记一条 agent_disconnect，
            # 并把还在跑的会话标成孤儿——而不是让它们悄悄烂在那儿。
            self._tasks.add(asyncio.ensure_future(self._close_bounded(self._ws)))
        self._loop.call_later(STOP_GRACE_S, self._cancel_run)

    async def _close_bounded(self, ws) -> None:
        """关连接，但别无限等。

        `ws.close()` 受 close_timeout 限制（默认 10 秒）；对面已经不通的时候
        那 10 秒会白等，所以再套一层更短的超时。
        """
        try:
            await asyncio.wait_for(ws.close(), 3.0)
        except Exception:
            pass                        # 关不掉也没关系，进程要退了

    def _cancel_run(self) -> None:
        """看门狗：宽限期内没退出才动硬的。"""
        task = self._run_task
        if task is not None and not task.done():
            task.cancel()

    @staticmethod
    def _close_code(exc: BaseException) -> int | None:
        """从异常里抠出 WebSocket 关闭码，抠不到返回 None。

        优先读 `rcvd.code`（websockets 13+ 的正式属性），退一步读 `sent.code`，
        都没有再从 repr 里找我们认识的那三个码——`ConnectionClosed` 的 repr
        里本来就带着 `Close(code=4401, ...)`。留这条兜底是因为这块跨
        websockets 版本变过（老的 `ConnectionClosed.code` 已经被弃用了）。
        """
        for side in ("rcvd", "sent"):
            code = getattr(getattr(exc, side, None), "code", None)
            if isinstance(code, int):
                return code
        text = repr(exc)
        for code in (CLOSE_BAD_AUTH, CLOSE_SUPERSEDED, CLOSE_NETWORK_DENIED):
            if str(code) in text:
                return code
        return None

    @staticmethod
    def _http_status(exc: BaseException) -> int | None:
        """握手被 HTTP 拒了的话拿到那个状态码。

        `InvalidStatus.response.status_code` 是正式属性；老版本只能从
        "server rejected WebSocket connection: HTTP 403" 这句里抠。
        """
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if isinstance(status, int):
            return status
        matched = re.search(r"HTTP (\d{3})", repr(exc))
        return int(matched.group(1)) if matched else None

    @staticmethod
    def _explain(exc: Exception) -> Failure:
        """把底层异常翻译成能直接照着做的提示，并说明还该不该重连。

        四种失败以前报的是同一句话（或者干脆是裸的 repr），而它们的处理
        方式完全不同，所以这里一个一个分开。
        """
        code = Agent._close_code(exc)

        # 密钥不对：改密钥需要人去动两边的配置，重连解决不了任何事。
        if code == CLOSE_BAD_AUTH:
            return Failure(
                "密钥不正确：与服务端 config.toml 中的 token 不一致。"
                "请重新运行 tools/gen_token.py --force，并重启两端",
                fatal=True)

        # 被同名顶掉：**绝不能重连**——连上去就把对方顶掉，对方再顶回来，
        # single_agent_id 默认开着，两条会一直互相踢，永远停不下来。
        # 这条通常从 `_serve` 里正常结束掉（那边也返回 False），这里是它
        # 万一以异常形式冒出来时的同一条出路。
        if code == CLOSE_SUPERSEDED:
            # 这句话会被拼在"连接断开："后面，所以别再自己说一遍"断开"。
            return Failure(
                "同一主机名已在另一处连接，本连接已被替换。"
                "若为本人操作，请关闭另一处的 agent 后重新启动本连接",
                fatal=True)

        # 网段：这条连接**已经握手成功过**才被关，所以地址本身是对的，
        # 问题出在本机地址不在服务器的 allowed_networks 里。
        if code == CLOSE_NETWORK_DENIED:
            return Failure(
                "本机地址不在服务器的 allowed_networks 内（4403）。"
                "可在服务器端界面点击「检测」确认")

        if Agent._http_status(exc) == 403:
            return Failure(
                "服务端拒绝了 WebSocket 握手（403）。可能原因有二："
                "① 地址有误（请确认 --server 为 ws://<对方局域网 IP>:8765，且对方 hub 正在运行）；"
                "② 本机地址不在服务器的 allowed_networks 内——hub 在握手前即拒绝，两者表现相同。"
                "可在服务器端界面点击「检测」区分具体原因")

        text = repr(exc)
        if isinstance(exc, ConnectionRefusedError) or "ConnectionRefusedError" in text \
                or "10061" in text:
            return Failure("连接被拒绝：对方 hub 尚未启动，或防火墙拦截了 8765 端口")
        if isinstance(exc, (TimeoutError, asyncio.TimeoutError)) \
                or "TimeoutError" in text or "timed out" in text.lower():
            return Failure("10 秒内未能连接：对方 hub 未启动，或中间存在防火墙/网段隔离")
        if "nodename nor servname" in text or "getaddrinfo" in text:
            return Failure("地址无法解析，请检查 --server 中的 IP 是否正确")
        return Failure(text)

    async def _serve(self, ws) -> bool:
        """一轮连接。返回 False 表示该彻底退出（鉴权失败等）。"""
        raw = await ws.recv()
        msg = json.loads(raw)
        if msg.get("t") != T_CHALLENGE:
            self.say(f"服务端未按协议发送挑战：{msg!r}")
            return False
        if msg.get("ver") != PROTO_VERSION:
            self.say(f"协议版本不匹配：服务端 {msg.get('ver')}，本地 {PROTO_VERSION}。"
                     f"请同步更新两端代码。")
            return False

        proof = hmac.new(self.token.encode("utf-8"),
                         str(msg["nonce"]).encode("utf-8"), hashlib.sha256).hexdigest()
        host_info = {
            "hostname": socket.gethostname(),
            "user": os.environ.get("USERNAME", ""),
            "os": "Windows",
            "arch": os.environ.get("PROCESSOR_ARCHITECTURE", ""),
            "cwd": os.getcwd(),
            "mode": self.mode,
            "shell": self.shell,
            "agent_pid": os.getpid(),
        }
        await ws.send(json.dumps({
            "t": T_AUTH, "ver": PROTO_VERSION, "proof": proof,
            "agent_id": self.name,
            "ui": self.ui,
            "host": host_info,
        }))

        raw = await ws.recv()
        msg = json.loads(raw)
        if msg.get("t") == T_ERROR:
            self.say(f"服务端拒绝：{msg.get('msg')}")
            return False
        if msg.get("t") != T_READY:
            self.say("鉴权失败（密钥可能不正确），正在退出。")
            return False

        code = msg.get("session_code", "------")
        # 握手完成，从这里开始才能往外发消息（见 send_chat 的说明）。
        # `chat` 是 hub 的附加能力标记：只有服务器端真的开了图形界面、挂着消息
        # 面板时才为真。旧 hub 不带这个键，按"不支持"处理——否则消息发出去就
        # 石沉大海，界面上却显示正常。
        self._chat_ready = msg.get("chat") is True
        self.say(f"已连接到 {self.server_url}")
        self.say(f"会话码 {code}：对端界面应显示相同数字；"
                 f"若不一致，请勿继续操作")
        self.say(f"模式 {self.mode}，默认 shell {self.shell}；按 Ctrl+C 断开并退出")
        self.audit.write("connect", server=self.server_url, session_code=code)
        # 界面靠这一条切到"已连接"，会话码也在这里给出去。
        self.emit("connected", server=self.server_url, session_code=code,
                  mode=self.mode, shell=self.shell, name=self.name, host=host_info,
                  chat=self._chat_ready)

        async for raw in ws:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            await self._handle(ws, data)

        # 4402 = 同一个机器名的 agent 在别处连上了，hub 把这条顶掉。
        # 这条**绝不能重连**：重连上去会把对方顶掉，对方再顶回来——
        # single_agent_id 默认开着，两条连接会一直互相踢，永远停不下来。
        if getattr(ws, "close_code", None) == CLOSE_SUPERSEDED:
            self._fatal_text = ("连接已断开：同一主机名已在另一处连接，本连接被替换。"
                                "若为本人操作，请关闭另一处的 agent 后重新启动本连接。")
            self.say(self._fatal_text)
            return False

        return True

    async def _handle(self, ws, msg: dict) -> None:
        kind = msg.get("t")
        handler = {
            T_EXEC: self._on_exec,
            T_STDIN: self._on_stdin,
            T_SIGNAL: self._on_signal,
            T_PUT_BEGIN: self._on_put_begin,
            T_PUT: self._on_put,
            T_PUT_END: self._on_put_end,
            T_GET: self._on_get,
            T_SHOT: self._on_shot,
            T_CLIP_GET: self._on_clip_get,
            T_CLIP_SET: self._on_clip_set,
            T_PING: self._on_ping,
            T_CHAT_SERVER: self._on_chat,
        }.get(kind)
        if handler is None:
            self.say(f"收到无法识别的消息类型 {kind!r}")
            return

        # 可能跑很久的操作必须丢到后台，直接在接收循环里 await 会把循环堵死——
        # 命令没结束就收不到 stdin、收不到中断、也收不到下一条命令。
        #
        # 上传的三个分片消息刻意留成同步处理，保证 begin/片/end 严格按序。
        if kind in _SLOW_OPS:
            self._spawn(self._guarded(ws, kind, handler, msg))
        else:
            await self._guarded(ws, kind, handler, msg)

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _guarded(self, ws, kind: str, handler, msg: dict) -> None:
        try:
            await handler(ws, msg)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.say(f"处理 {kind} 时发生错误：{exc!r}")
            await self._err(ws, msg, repr(exc))

    async def _err(self, ws, msg: dict, text: str) -> None:
        try:
            await ws.send(json.dumps({"t": T_ERROR, "session": msg.get("session", "main"),
                                      "id": msg.get("id"), "msg": text},
                                     ensure_ascii=False))
        except Exception:
            pass

    # -- 执行命令 -----------------------------------------------------------
    async def _on_exec(self, ws, msg: dict) -> None:
        sid = msg.get("session") or "main"
        sess0 = self.session(sid)
        async with sess0.lock:
            await self._exec_locked(ws, msg, sid)

    async def _exec_locked(self, ws, msg: dict, sid: str) -> None:
        cmd = msg.get("cmd", "")
        sess = self.session(sid)
        shell = msg.get("shell") or sess.shell
        cwd = msg.get("cwd") or sess.cwd

        # 在**任何**拒绝之前发出去：对方要能看到"有人试图执行这条命令"，
        # 包括被会话忙、路径越界、策略拒绝挡掉的那些。这是"全程可见"的核心一条。
        self.emit("cmd", session=sid, cmd=cmd, cwd=cwd)

        if sess.proc is not None and sess.proc.returncode is None:
            await self._send(ws, {"t": T_DENIED, "session": sid, "id": msg.get("id"),
                                  "reason": "该会话正忙"})
            return

        ok, why = check_path(cwd, self.allow_root)
        if not ok:
            await self._send(ws, {"t": T_DENIED, "session": sid, "id": msg.get("id"),
                                  "reason": why})
            self.say(f"$ {cmd}   -> 已拒绝（{why}）")
            return

        action, reason, pattern = check_command(cmd, self.mode)
        self.say(f"$ {cmd}")

        if action == DENY:
            self.say(f"  -> 已拒绝：{reason}")
            self.audit.write("deny", session=sid, cmd=cmd, reason=reason, pattern=pattern,
                             cwd=cwd)
            await self._send(ws, {"t": T_DENIED, "session": sid, "id": msg.get("id"),
                                  "reason": reason, "pattern": pattern})
            return

        if action == CONFIRM:
            self.say(f"  -> 等待本机确认（{reason}）…")
            # 结构化地告诉界面"现在有个系统弹窗挂在你屏幕上"。界面靠这个在顶上
            # 显一条红条、并把窗口标题改掉，而不是去匹配上面那句话的文字——
            # 匹配字符串的写法改一个字就悄悄失效了。
            self.emit("confirm_pending", session=sid, cmd=cmd, reason=reason)
            approved = await asyncio.to_thread(ask_confirm, cmd, reason, self.confirm_timeout)
            self.emit("confirm_done", session=sid, approved=bool(approved))
            self.audit.write("confirm", session=sid, cmd=cmd, reason=reason,
                             approved=approved, cwd=cwd)
            if not approved:
                self.say("  -> 已拒绝执行")
                await self._send(ws, {"t": T_DENIED, "session": sid, "id": msg.get("id"),
                                      "reason": f"对方拒绝了：{reason}"})
                return
            self.say("  -> 已确认，开始执行")

        if not os.path.isdir(cwd):
            cwd = self.default_cwd
        sess.cwd = cwd
        sess.shell = shell

        try:
            argv, _ = build_launch(cmd, cwd, shell)
        except ValueError as exc:
            await self._send(ws, {"t": T_DENIED, "session": sid, "id": msg.get("id"),
                                  "reason": str(exc)})
            return

        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        # 独立进程组（好收 CTRL_BREAK）+ 自己没控制台时不要给子进程弹新窗口。
        # 见 child_creationflags()。
        creationflags = child_creationflags()

        started = time.time()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd, env=env, creationflags=creationflags,
            )
        except Exception as exc:
            self.say(f"  -> 进程启动失败：{exc!r}")
            await self._err(ws, msg, f"进程启动失败：{exc!r}")
            return

        sess.proc, sess.cmd_id = proc, msg.get("id")

        pumps = [
            asyncio.create_task(
                self._pump(ws, sid, proc.stdout, "stdout", with_sentinel=True)),
            asyncio.create_task(
                self._pump(ws, sid, proc.stderr, "stderr", with_sentinel=False)),
        ]

        rc = await proc.wait()
        results = await asyncio.gather(*pumps, return_exceptions=True)
        tail = results[0] if results and isinstance(results[0], str) else ""
        # 一定要写回 sess.cwd：只把新目录塞进 done 消息的话，
        # 下一条命令还会在旧目录里跑，而 hub 那边显示的却是新目录。
        sess.cwd = cwd_after = self._remember_cwd(sess, tail, cwd)
        sess.proc = None

        elapsed = time.time() - started
        self.say(f"  -> 执行结束，退出码 {rc}（{elapsed:.1f}s）"
                 + (f"，当前目录 {cwd_after}" if cwd_after else ""))
        self.audit.write("exec", session=sid, cmd=cmd, exit=rc, cwd=cwd_after,
                         seconds=round(elapsed, 2))

        await self._send(ws, {"t": T_DONE, "session": sid, "id": sess.cmd_id,
                              "exit": rc, "cwd": cwd_after})

    def _remember_cwd(self, sess: Session, tail: str, fallback: str) -> str:
        """从缓冲区尾巴里抠出哨兵行，取出命令跑完时的工作目录。

        命令被强杀、或者哨兵没来得及打出来（比如命令自己调了 exit）时，
        退回命令开始前的目录。
        """
        matches = list(SENTINEL_RE.finditer(tail))
        if not matches:
            return fallback
        return matches[-1].group(2).strip() or fallback

    async def _pump(self, ws, sid: str, stream, name: str, with_sentinel: bool) -> str:
        """把子进程输出流式转发回去。

        尾巴上留一段先不发，因为哨兵行可能还没收全。结束时空闲下来的那段
        再吐出去（哨兵行本身剥掉）。
        """
        decoder = StickyDecoder()
        held = ""

        async def send(text: str) -> None:
            if text:
                # 先给界面，再上线。`_send` 会把发送失败吞掉（连接断了），
                # 本地显示不该跟着一起丢——命令确实跑过，输出确实产生过。
                self.emit("output", session=sid, stream=name, text=text)
                await self._send(ws, {"t": T_CHUNK, "session": sid,
                                      "stream": name, "data": text})

        while True:
            data = await stream.read(4096)
            if not data:
                break
            held += decoder.feed(data)
            if not with_sentinel:
                await send(held)
                held = ""
                continue
            if len(held) > HOLD_BACK:
                await send(held[:-HOLD_BACK])
                held = held[-HOLD_BACK:]

        held += decoder.flush()

        if not with_sentinel:
            await send(held)
            return ""

        matches = list(SENTINEL_RE.finditer(held))
        if matches:
            last = matches[-1]
            await send(held[:last.start()])
            after = held[last.end():].lstrip("\r\n")
            await send(after)
        else:
            await send(held)
        return held

    async def _on_stdin(self, ws, msg: dict) -> None:
        sid = msg.get("session") or "main"
        sess = self.sessions.get(sid)
        if sess is None or sess.proc is None or sess.proc.stdin is None:
            await self._err(ws, msg, "当前没有正在运行的进程，无法写入标准输入")
            return
        try:
            sess.proc.stdin.write(msg.get("data", "").encode("utf-8"))
            await sess.proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, RuntimeError) as exc:
            await self._err(ws, msg, f"标准输入已关闭：{exc!r}")

    async def _on_signal(self, ws, msg: dict) -> None:
        sid = msg.get("session") or "main"
        sess = self.sessions.get(sid)
        if sess is None or sess.proc is None:
            await self._err(ws, msg, "当前没有正在运行的进程")
            return
        proc, sig = sess.proc, msg.get("sig", "ctrl-c")
        pid = proc.pid
        self.say(f"收到中断请求（{sig}），正在终止 PID {pid}")

        if sig == "kill" or os.name != "nt":
            await asyncio.to_thread(_kill_tree, pid)
            return

        # 先礼后兵：CTRL_BREAK 是 Windows 上唯一能定向发给某个进程组的信号
        # （CTRL_C_EVENT 做不到精准投递），脚本可以捕获它做收尾工作。
        sent = False
        try:
            os.kill(pid, signal.CTRL_BREAK_EVENT)
            sent = True
        except OSError as exc:
            self.say(f"  -> 无法发送 CTRL_BREAK（{exc!r}），改为强制终止")

        # 但控制事件不是万能的：进程没挂控制台、或者正卡在 Start-Sleep 这种
        # 不响应控制事件的地方时，GenerateConsoleCtrlEvent 会"成功但没效果"。
        # 所以必须等一小会儿确认，还没死就补一刀——中断必须是可靠的。
        if sent:
            try:
                await asyncio.wait_for(proc.wait(), CTRL_BREAK_GRACE_S)
                self.say(f"  -> PID {pid} 已响应中断并退出")
                return
            except asyncio.TimeoutError:
                self.say(f"  -> {CTRL_BREAK_GRACE_S:.0f} 秒内无响应，改为强制终止")

        await asyncio.to_thread(_kill_tree, pid)

    # -- 文件 ---------------------------------------------------------------
    async def _on_put_begin(self, ws, msg: dict) -> None:
        tid, path = msg["transfer_id"], msg["path"]
        ok, why = check_path(path, self.allow_root)
        if not ok:
            await self._file_done(ws, tid, False, why)
            return
        self._uploads[tid] = {"path": path, "data": bytearray(),
                              "sha256": msg.get("sha256"), "size": msg.get("size", 0)}

    async def _on_put(self, ws, msg: dict) -> None:
        up = self._uploads.get(msg.get("transfer_id", ""))
        if up is None:
            return
        up["data"] += base64.b64decode(msg.get("data", ""))
        # 对方声称的大小不设防的话，一个超大文件能把内存吃光
        if len(up["data"]) > MAX_TRANSFER_BYTES:
            self._uploads.pop(msg["transfer_id"], None)
            await self._file_done(ws, msg["transfer_id"], False, "文件超过大小上限")

    async def _on_put_end(self, ws, msg: dict) -> None:
        tid = msg.get("transfer_id", "")
        up = self._uploads.pop(tid, None)
        if up is None:
            await self._file_done(ws, tid, False, "未找到对应的上传会话")
            return

        blob = bytes(up["data"])
        digest = hashlib.sha256(blob).hexdigest()
        if up["sha256"] and digest != up["sha256"]:
            await self._file_done(ws, tid, False, "校验和不匹配，文件可能已损坏")
            return

        dest = Path(up["path"])
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(blob)
        except OSError as exc:
            await self._file_done(ws, tid, False, f"写入失败：{exc}")
            return

        self.say(f"收到文件 {dest}（{len(blob)} 字节）")
        self.audit.write("put", path=str(dest), size=len(blob), sha256=digest)
        await self._file_done(ws, tid, True, str(dest), sha256=digest)

    async def _on_get(self, ws, msg: dict) -> None:
        tid, path = msg["transfer_id"], msg["path"]
        limit = int(msg.get("max_bytes") or MAX_TRANSFER_BYTES)

        ok, why = check_path(path, self.allow_root)
        if not ok:
            await self._file_done(ws, tid, False, why)
            return
        src = Path(path)
        if not src.is_file():
            await self._file_done(ws, tid, False, f"文件不存在：{path}")
            return
        size = src.stat().st_size
        if size > limit:
            await self._file_done(ws, tid, False, f"文件大小 {size} 字节，超过上限 {limit} 字节")
            return

        blob = src.read_bytes()
        digest = hashlib.sha256(blob).hexdigest()
        self.say(f"发送文件 {src}（{size} 字节）")

        await self._send(ws, {"t": T_FILE_BEGIN, "transfer_id": tid,
                              "size": size, "sha256": digest})
        for seq, off in enumerate(range(0, size, FILE_CHUNK_SIZE)):
            piece = blob[off:off + FILE_CHUNK_SIZE]
            await self._send(ws, {"t": T_FILE_CHUNK, "transfer_id": tid, "seq": seq,
                                  "data": base64.b64encode(piece).decode("ascii")})
        await self._file_done(ws, tid, True, str(src), sha256=digest)
        self.audit.write("get", path=str(src), size=size, sha256=digest)

    async def _file_done(self, ws, tid: str, ok: bool, msg: str = "", **extra) -> None:
        await self._send(ws, {"t": T_FILE_DONE, "transfer_id": tid,
                              "ok": ok, "msg": msg, **extra})

    # -- 屏幕 / 剪贴板 -------------------------------------------------------
    async def _on_shot(self, ws, msg: dict) -> None:
        tid = msg.get("transfer_id", "")
        script = (
            "Add-Type -AssemblyName System.Windows.Forms,System.Drawing\n"
            "$b = [System.Windows.Forms.SystemInformation]::VirtualScreen\n"
            "$bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height\n"
            "$g = [System.Drawing.Graphics]::FromImage($bmp)\n"
            "$g.CopyFromScreen($b.Left, $b.Top, 0, 0, $bmp.Size)\n"
            "$ms = New-Object System.IO.MemoryStream\n"
            "$bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)\n"
            "[Convert]::ToBase64String($ms.ToArray())\n"
            "$g.Dispose(); $bmp.Dispose(); $ms.Dispose()\n"
        )
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        try:
            proc = await asyncio.create_subprocess_exec(
                "powershell", "-NoProfile", "-EncodedCommand", encoded,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                creationflags=child_creationflags())
            out, err = await proc.communicate()
            if proc.returncode != 0:
                raise RuntimeError(err.decode("utf-8", errors="replace")[:500])
            data = out.decode("ascii", errors="ignore").strip()
            self.say(f"截屏 {len(data) * 3 // 4} 字节")
            self.audit.write("screenshot", bytes=len(data) * 3 // 4)
            await self._send(ws, {"t": T_SHOT_DATA, "transfer_id": tid, "data": data})
        except Exception as exc:
            await self._file_done(ws, tid, False, f"截屏失败：{exc!r}")

    async def _run_ps(self, script: str) -> str:
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        proc = await asyncio.create_subprocess_exec(
            "powershell", "-NoProfile", "-STA", "-EncodedCommand", encoded,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            creationflags=child_creationflags())
        out, err = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(err.decode("utf-8", errors="replace")[:300])
        return out.decode("utf-8", errors="replace")

    async def _on_clip_get(self, ws, msg: dict) -> None:
        tid = msg.get("transfer_id", "")
        try:
            text = await self._run_ps("Get-Clipboard -Raw")
            self.audit.write("clip_get", chars=len(text))
            await self._send(ws, {"t": T_CLIP_DATA, "transfer_id": tid, "text": text})
        except Exception as exc:
            await self._file_done(ws, tid, False, f"读剪贴板失败：{exc!r}")

    async def _on_clip_set(self, ws, msg: dict) -> None:
        tid = msg.get("transfer_id", "")
        text = msg.get("text", "")
        payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
        try:
            await self._run_ps(
                f"$t = [Text.Encoding]::UTF8.GetString("
                f"[Convert]::FromBase64String('{payload}')); Set-Clipboard -Value $t")
            self.say(f"已写入剪贴板（{len(text)} 字符）")
            self.audit.write("clip_set", chars=len(text))
            await self._file_done(ws, tid, True)
        except Exception as exc:
            await self._file_done(ws, tid, False, f"写剪贴板失败：{exc!r}")

    async def _on_ping(self, ws, msg: dict) -> None:
        await self._send(ws, {"t": T_PONG, "ts": msg.get("ts")})

    async def _on_chat(self, ws, msg: dict) -> None:
        """服务器端发来的一条消息。只交给界面，不回执、不进审计日志。"""
        text = normalize_chat_text(msg.get("text"))
        if text is None:
            return
        ts = msg.get("ts")
        self.emit("chat", text=text,
                  ts=ts if isinstance(ts, (int, float)) else time.time())

    def send_chat(self, text: str) -> bool:
        """从界面线程发一条消息给对方。返回是否已排进发送队列。

        和 stop() 同一条路：界面线程只把工作丢给 agent 的事件循环，自己绝不碰
        socket。`_chat_ready` 是必需的——握手完成前 `_ws` 也已经有值，那时候插
        一条消息进去会把 auth 帧挤到后面，hub 会直接把连接关掉。
        """
        text = normalize_chat_text(text)
        if text is None:
            return False
        loop = self._loop
        if loop is None or loop.is_closed() or self._ws is None or not self._chat_ready:
            return False
        try:
            loop.call_soon_threadsafe(self._send_chat_now, text)
        except RuntimeError:          # 循环正在关
            return False
        return True

    def _send_chat_now(self, text: str) -> None:
        """在 agent 的事件循环里执行。"""
        ws = self._ws
        if ws is None or not self._chat_ready:
            return
        self._spawn(self._send(ws, {"t": T_CHAT, "text": text, "ts": time.time()}))

    async def _send(self, ws, msg: dict) -> None:
        try:
            await ws.send(json.dumps(msg, ensure_ascii=False))
        except Exception:
            pass  # 连接断了，重连逻辑会兜住

    async def _cleanup_children(self) -> None:
        """断开后不留孤儿进程，也不留悬空的后台任务。"""
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()

        for sess in self.sessions.values():
            if sess.proc is not None and sess.proc.returncode is None:
                await asyncio.to_thread(_kill_tree, sess.proc.pid)
                sess.proc = None
        self.sessions.clear()
        self._uploads.clear()


def _kill_tree(pid: int) -> None:
    """连同子孙进程一起杀。Windows 上必须用 taskkill /T，否则只死一个壳。"""
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, timeout=15, check=False)
            return
        except Exception:
            pass
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


# ===========================================================================
# 入口
# ===========================================================================
def normalize_server_url(url: str) -> str:
    """把 ws://ip:8765 补成 ws://ip:8765/agent。

    不加这一步的话，路径为空会连到 `/`，服务端没有对应的 WebSocket 路由，
    直接回一个 403——报错信息完全看不出是这个原因。
    """
    from urllib.parse import urlsplit, urlunsplit

    url = url.strip()
    if "://" not in url:
        url = "ws://" + url
    parts = urlsplit(url)
    path = parts.path
    if path in ("", "/"):
        path = "/agent"
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


def load_config(args) -> dict:
    cfg: dict = {}
    if args.config:
        path = Path(args.config)
        if not path.is_file():
            sys.exit(f"配置文件不存在：{path}")
        with path.open("rb") as fh:
            cfg = dict(tomllib.load(fh).get("agent", {}))

    if args.server:
        cfg["server_url"] = args.server
    if args.token:
        cfg["token"] = args.token
    if args.mode:
        cfg["mode"] = args.mode
    if args.allow_root:
        cfg["allow_root"] = args.allow_root
    if args.shell:
        cfg["shell"] = args.shell
    if args.max_lifetime:
        cfg["max_lifetime"] = args.max_lifetime
    if args.confirm_timeout:
        cfg["confirm_timeout_s"] = args.confirm_timeout
    if args.name:
        cfg["name"] = args.name

    if not cfg.get("server_url"):
        sys.exit("必须指定 --server，例如 ws://10.22.114.127:8765")
    cfg["server_url"] = normalize_server_url(cfg["server_url"])
    if not cfg.get("token"):
        sys.exit("必须指定 --token（须与服务端 config.toml 中的 token 一致）")
    if cfg.get("mode", MODE_CONFIRM) not in MODES:
        sys.exit(f"mode 只能取 {MODES} 之一")
    return cfg


def main() -> None:
    ap = argparse.ArgumentParser(
        description="远程终端 agent（运行于被控电脑）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="例：python agent.py --server ws://10.22.114.127:8765 --token abc123")
    ap.add_argument("--config", help="TOML 配置文件路径")
    ap.add_argument("--server", help="服务端地址，如 ws://10.22.114.127:8765")
    ap.add_argument("--token", help="预共享密钥")
    ap.add_argument("--mode", choices=MODES,
                    help="safe=只读；confirm=危险命令弹出确认框（默认）；auto=不弹出确认框")
    ap.add_argument("--allow-root", help="限制文件操作与 cd 仅能在这些目录下进行")
    ap.add_argument("--shell", choices=["powershell", "pwsh", "cmd", "bash"],
                    help="shell 类型（默认 powershell）")
    ap.add_argument("--max-lifetime", type=float,
                    help="最长运行时间（秒），到时自动退出；不填表示不限")
    ap.add_argument("--confirm-timeout", type=int,
                    help="危险命令确认框的超时时间（秒），超时视为拒绝，默认 60")
    ap.add_argument("--name", help="本机标识，多台设备时用于区分")
    args = ap.parse_args()

    cfg = load_config(args)
    agent = Agent(cfg)

    print("=" * 68)
    print("  远程终端 agent")
    print(f"  本机    : {socket.gethostname()}  ({os.environ.get('USERNAME', '')})")
    print(f"  服务端  : {cfg['server_url']}")
    print(f"  模式    : {agent.mode}"
          + ("（只读）" if agent.mode == MODE_SAFE else ""))
    if agent.allow_root:
        print(f"  目录限制: {agent.allow_root}")
    print(f"  审计日志: {agent.audit.path}")
    print("  每条命令都会记录在此；危险命令将弹出确认框。")
    print("  按 Ctrl+C 立即退出。")
    print("=" * 68)

    try:
        asyncio.run(agent.run())
    except KeyboardInterrupt:
        print("\n已退出。")
    finally:
        agent.audit.write("disconnect")


if __name__ == "__main__":
    # stdout 被重定向到文件/管道时，Windows 上 Python 会退回本地代码页（cp936），
    # 中文日志就变成 GBK——跟项目里其他一律 UTF-8 的地方对不上，事后翻日志很痛苦。
    # 真实控制台走的是 WriteConsoleW，不看这个编码，所以改了不会让窗口变乱码。
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    # Windows 上要用 Proactor 才能跑 asyncio 子进程
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    main()

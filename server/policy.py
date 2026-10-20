"""危险命令判定。

为什么要有这层：真正会造成不可逆后果的通常不是恶意，而是**驱动这套东西的 LLM 手滑**——
它可能把 `rm -rf build` 写成 `rm -rf /`，或者在错误的工作目录下执行删除。
所以策略在**两端各跑一遍**（agent 侧是最终否决权，server 侧是发送前护栏），
任何一侧命中就拦。

判定结果三选一：
  allow   —— 直接执行
  confirm —— 弹窗让对面的人点"允许/拒绝"
  deny    —— 直接拒绝，不给确认机会

三种模式：
  safe    —— 只放行只读命令，其余一律 deny（起步时用来验证链路）
  confirm —— 默认。黑名单 deny，灰名单 confirm，其余 allow
  auto    —— 黑名单 deny，其余 allow（确认框全关，风险自负）
"""

from __future__ import annotations

import re
from dataclasses import dataclass

ALLOW = "allow"
CONFIRM = "confirm"
DENY = "deny"

MODE_SAFE = "safe"
MODE_CONFIRM = "confirm"
MODE_AUTO = "auto"
MODES = (MODE_SAFE, MODE_CONFIRM, MODE_AUTO)


# ---------------------------------------------------------------------------
# 永远拒绝：没有"这次是例外"的余地。任何模式下都拦，连确认框都不弹。
# ---------------------------------------------------------------------------
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
    # 删盘符根目录：rd /s /q C:\  、 Remove-Item -Recurse -Force C:\
    (r"\b(?:rd|rmdir)\s+/s\s+/q\s+[a-z]:\\?\s*$", "递归删除整个盘符"),
    (r"\bdel\s+/[fsq].*?\b[a-z]:\\\*\s*$", "递归删除整个盘符内容"),
    (r"remove-item\b(?=.*-recurse)(?=.*-force).*?\b[a-z]:\\?\s*(\||;|$)", "递归强制删除盘符根目录"),
    # 保护区：Windows / Program Files / 用户目录整体
    (r"\b(?:rd|rmdir)\s+/s\s+/q\s+\"?[a-z]:\\(?:windows|program files)",
     "递归删除系统目录"),
    (r"remove-item\b(?=.*-recurse)(?=.*-force).*?\\(?:windows|program files)\b",
     "递归强制删除系统目录"),
]

# ---------------------------------------------------------------------------
# 需要人工确认：合法用途存在，但不该由 AI 单方面决定
# ---------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# safe 模式下的只读白名单：命令必须以其中之一开头
# ---------------------------------------------------------------------------
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


@dataclass(frozen=True)
class Decision:
    action: str          # ALLOW / CONFIRM / DENY
    label: str = ""      # 命中原因，用于弹窗和日志
    pattern: str = ""    # 命中的正则原文，便于事后调规则

    @property
    def ok(self) -> bool:
        return self.action != DENY


def _compile(patterns: list[tuple[str, str]]) -> list[tuple[re.Pattern[str], str, str]]:
    return [(re.compile(p, re.IGNORECASE), label, p) for p, label in patterns]


_DENY = _compile(DENY_PATTERNS)
_CONFIRM = _compile(CONFIRM_PATTERNS)
_SAFE = [re.compile(p, re.IGNORECASE) for p in SAFE_PATTERNS]


def _first_match(
    compiled: list[tuple[re.Pattern[str], str, str]], cmd: str
) -> tuple[str, str] | None:
    for rx, label, raw in compiled:
        if rx.search(cmd):
            return label, raw
    return None


def check_command(cmd: str, mode: str = MODE_CONFIRM) -> Decision:
    """判定一条命令该怎么处理。

    注意：这是基于正则的**启发式**，不是安全边界。它拦的是常见的手滑，不是蓄意规避。
    真正的边界是——对面那个人能看见每一条命令，并且随时能点拒绝。
    """
    if mode not in MODES:
        raise ValueError(f"未知模式 {mode!r}，应为 {MODES}")

    cmd = (cmd or "").strip()
    if not cmd:
        return Decision(DENY, "空命令")

    hit = _first_match(_DENY, cmd)
    if hit:
        return Decision(DENY, hit[0], hit[1])

    if mode == MODE_SAFE:
        # 每一段都必须是认识的只读命令，整条才算安全
        segs = _segments(cmd)
        if segs and all(any(rx.match(s) for rx in _SAFE) for s in segs):
            return Decision(ALLOW)
        hit = _first_match(_CONFIRM, cmd)
        if hit:
            return Decision(DENY, f"safe 模式只允许只读命令（{hit[0]}）", hit[1])
        return Decision(DENY, "safe 模式只允许只读命令")

    hit = _first_match(_CONFIRM, cmd)
    if hit:
        if mode == MODE_AUTO:
            return Decision(ALLOW, f"[auto 模式放行] {hit[0]}", hit[1])
        return Decision(CONFIRM, hit[0], hit[1])

    return Decision(ALLOW)


def check_path(path: str, allow_root: str | None) -> Decision:
    """路径牢笼：解析成真实路径后必须落在 allow_root 之内。

    用 realpath 而不是字符串前缀比较，否则 `C:\\allow\\..\\..\\Windows` 和
    指向外部的符号链接都能绕过去。（这个思路参考了 dsh-file 的 resolveInside。）
    """
    import os

    if not allow_root:
        return Decision(ALLOW)
    if not path:
        return Decision(DENY, "空路径")

    try:
        root = os.path.realpath(os.path.abspath(allow_root))
        target = os.path.realpath(os.path.abspath(path))
    except OSError as exc:
        return Decision(DENY, f"路径解析失败: {exc}")

    if target == root:
        return Decision(ALLOW)

    sep = os.sep
    if os.name == "nt":
        # Windows 路径大小写不敏感，比较前统一大小写
        if target.lower().startswith(root.lower().rstrip(sep) + sep):
            return Decision(ALLOW)
    elif target.startswith(root.rstrip(sep) + sep):
        return Decision(ALLOW)

    return Decision(DENY, f"路径越界: {target} 不在 {root} 之内")

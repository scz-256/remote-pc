"""改 config.toml 里的密钥，别把人家的文件改坏。

`tomllib` 只能读不能写。为这一件事引一个写库不值当（而且大多数写库会把注释和排版
全冲掉）。所以这里只做一件事：**只替换 token 那一行，其余逐字节保留**。

为什么在乎这个：操作者的 config.toml 里有他自己写的注释、调过的 `allowed_networks`、
可能还改过 `exec_default_timeout_s`。把整个文件重写成模板，和"凭空换一把新密钥"
是同一类静默破坏——当时看不出来，几天后以"某个功能怎么不管用了"的形式冒出来。

`NEW_CONFIG_TEMPLATE` 也放这儿，由 `tools/gen_token.py` 共用，保证只有一份模板。
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

SECTION_RE = re.compile(r"^\s*\[([^\]]+)\]\s*$")
# 只认最基本的 token = "..." / token = '...' / token = 裸值，行尾可以有注释
TOKEN_LINE_RE = re.compile(r'^(\s*token\s*=\s*)(?:"[^"]*"|\'[^\']*\'|\S+)(\s*(?:#.*)?)$')

BACKUP_SUFFIX = ".bak"


class ConfigEditError(RuntimeError):
    """改不了。消息是给用户看的，要说清楚为什么不敢动。"""


NEW_CONFIG_TEMPLATE = """# 远程终端 hub 配置

[server]
# 预共享密钥。agent 用它做 HMAC 握手应答，密钥本身不上网。
token = "{token}"

# 监听地址。0.0.0.0 表示所有网卡，局域网内可访问；
# 只本机测试时改成 127.0.0.1。
host = "0.0.0.0"
port = 8765

# 只接受这些网段连进来。默认都是私有地址，公网 IP 会被直接拒绝。
allowed_networks = ["127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]

# 服务端这一侧的策略：safe / confirm / auto
#   safe    —— 只放行只读命令，用来先验证链路
#   confirm —— 危险命令交给对方确认（默认）
#   auto    —— 不弹框（服务端这一层也放行；agent 侧仍按它自己的模式执行）
mode = "confirm"

# 命令默认等多久算超时（秒）。超时不杀进程，只是先把已收到的输出返回。
exec_default_timeout_s = 120.0

# 没连上 agent 时，API 直接报错而不是干等
require_agent = true

# 同一个 agent 重连时，把旧连接顶掉
single_agent_id = true

# CLI / MCP server 从这里找 hub
hub_url = "http://127.0.0.1:8765"
"""


def quote(value: str) -> str:
    """写成 TOML 基本字符串。

    密钥里出现引号或反斜杠的概率极低，但真出现时不该写坏一个文件。
    """
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_new_config(token: str) -> str:
    """一份全新的 config.toml。"""
    return NEW_CONFIG_TEMPLATE.format(token=token)


def set_token_in_text(text: str, token: str) -> str:
    """把 `[server]` 段里的 token 换成新的，其余原样返回。

    找不到 `[server]` 段就报错，**不猜**——猜错了就是把人家文件改坏。
    找不到 token 行（比如手写漏了）就插在 `[server]` 表头下面一行。
    """
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()

    section: str | None = None
    server_at: int | None = None
    token_at: int | None = None

    for index, line in enumerate(lines):
        header = SECTION_RE.match(line)
        if header:
            section = header.group(1).strip()
            if section == "server" and server_at is None:
                server_at = index
            continue
        # 只看 [server] 段内部的 token。写成 [server.something] 的小节不会被误判，
        # 因为那时 section 是 "server.something"。
        if section == "server" and token_at is None and TOKEN_LINE_RE.match(line):
            token_at = index

    if server_at is None:
        raise ConfigEditError(
            "这个文件里没有 [server] 段，我不敢猜着改。"
            "可以直接删掉它，让程序重新生成一份。")

    if token_at is not None:
        matched = TOKEN_LINE_RE.match(lines[token_at])
        assert matched is not None  # 上面 match 过了
        lines[token_at] = matched.group(1) + quote(token) + matched.group(2)
    else:
        lines.insert(server_at + 1, f"token = {quote(token)}")

    return newline.join(lines) + newline


def write_config(path: str | Path, text: str) -> None:
    """原子地把内容写进去，顺手留一份 .bak。

    先写临时文件再 `os.replace`：中途断电/被杀时，要么是旧内容、要么是新内容，
    不会留下一个半截的 config.toml——那个下一轮启动会直接读失败。
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    tmp = target.with_name(target.name + ".tmp")
    # newline="" —— 不让 Python 把 "\n" 翻译成 os.linesep。Windows 上默认会翻译，
    # 于是一份原先是 LF 的文件被保存一次就整篇变成 CRLF；而"只动 token 那一行"
    # 的承诺也就没了（diff 会显示每一行都变了）。
    with tmp.open("w", encoding="utf-8", newline="") as fh:
        fh.write(text)

    # 顺序有讲究：先把新内容整篇落到临时文件，再碰原件。
    # 反过来的话，"目录读得了、写不了"会在动完原件之后才失败，
    # 于是原件只剩那份 .bak —— 用户以为自己在改配置，其实把配置弄丢了。
    # 备份也用 copy2 而不是 replace，理由同上：原件在原地待到最后一步。
    if target.is_file():
        try:
            shutil.copy2(target, target.with_name(target.name + BACKUP_SUFFIX))
        except OSError:
            pass          # 备份失败不该挡着正事

    try:
        os.replace(tmp, target)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


@dataclass(frozen=True)
class SaveResult:
    """一次"把密钥写回配置文件"的结果。成败都在这里，不靠异常。"""

    ok: bool
    path: Path | None = None
    text: str = ""                     # 一句话，界面直接显示
    backup: Path | None = None
    created: bool = False              # 之前没有这个文件，是这次新建的


def save_token_to_config(path: str | Path, token: str) -> SaveResult:
    """把新密钥写进 `path`。**不抛异常**——成败都用 `SaveResult` 说清楚。

    两条硬规矩：

    * **失败绝不改写到别的地方去。** 操作者机器上那份 config.toml 是 hub、
      CLI、MCP 共用的。这里"写不进去就退到用户目录"等于凭空多出第二把密钥，
      现象是几天后「CC 忽然说 token 无效」，很难往回追。宁可什么都不写，
      把原因原样报出来——界面上红字显示，人自己决定怎么办。
    * **只动 token 那一行**，其余逐字节保留（见 `set_token_in_text`）。
    """
    target = Path(path)
    existed = target.is_file()
    try:
        if existed:
            # 用 open 而不是 Path.read_text(newline=...)：那个参数要 3.13，
            # 这个项目跑 3.12。读的时候也不能翻译换行，否则写回去整篇 LF 变 CRLF。
            with target.open("r", encoding="utf-8", newline="") as fh:
                raw = fh.read()
            text = set_token_in_text(raw, token)
        else:
            text = render_new_config(token)
    except ConfigEditError as exc:
        return SaveResult(False, target, str(exc))
    except UnicodeDecodeError:
        return SaveResult(False, target,
                          f"{target} 不是 UTF-8 文本，我不敢改它。"
                          f"把它删掉或另存一份，再重新生成密钥。")
    except OSError as exc:
        return SaveResult(False, target, f"读不了 {target}：{exc}")

    try:
        write_config(target, text)
    except OSError as exc:
        return SaveResult(False, target,
                          f"写不了 {target}：{exc}。"
                          f"密钥只在本次会话里有效，配置文件没动。")

    if existed:
        msg = f"已写入 {target}（旧文件存为 {target.name}{BACKUP_SUFFIX}）"
    else:
        msg = f"已新建 {target}"
    return SaveResult(True, target, msg,
                      backup=target.with_name(target.name + BACKUP_SUFFIX) if existed else None,
                      created=not existed)

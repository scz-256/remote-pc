"""连接串的编解码。

服务器端窗口把这一串给出去，客户端**粘贴一次**就够了：

    ws://10.22.114.127:8765#<config.toml 里那把密钥>

密钥不进 exe，所以换密钥只要重新粘一次，不用重新发文件。

解析比想象中啰嗦，因为人会粘进来的东西五花八门：可能是这一串，
也可能是整条命令行（`--server ws://... --token ...`），还可能是聊天记录里
带前后文的一整段。所以拆成两层：
    extract()  —— 永远不抛，能抠多少抠多少（界面先用它把字段填上）
    parse()    —— 在 extract 之上做校验，够不够用、合不合法
这样"只粘了个地址"也帮得上忙，而不是一句"格式不对"就完了。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

DEFAULT_PORT = 8765

# 粘贴进来的可能是一整段聊天记录，所以 --server / --token 要在全文里找
_SERVER_ARG_RE = re.compile(r"--server[=\s]+(\S+)", re.IGNORECASE)
_TOKEN_ARG_RE = re.compile(r"--token[=\s]+(\"[^\"]*\"|'[^']*'|\S+)", re.IGNORECASE)

_SCHEME_RE = re.compile(r"^(?:wss?|https?)://", re.IGNORECASE)

# 地址后面可能被顺手带上的路径，这些都不属于 host
_TRAILING_PATHS = ("/agent", "/")


class ConnInfoError(ValueError):
    """连接串看不懂 / 不够用。消息是直接给用户看的，要说清楚是哪一段的问题。"""


def _split_host_port(text: str) -> tuple[str | None, str | None]:
    """拆出 (host, 端口原文)。

    端口留原文不转 int —— 转不了的时候要能把 `abc` 这三个字写进报错里，
    而不是含糊地说"端口不对"。
    """
    text = text.strip()
    if not text:
        return None, None
    if text.startswith("["):                       # IPv6 字面量：[::1]:8765
        end = text.find("]")
        if end < 0:
            return None, None
        host = text[: end + 1]
        rest = text[end + 1:]
        return host, rest[1:] if rest.startswith(":") else None
    if ":" in text:
        host, _, port = text.rpartition(":")
        return (host or None), (port or None)
    return text, None


def _strip_fragment(text: str) -> tuple[str, str | None]:
    """按**第一个** # 切开。token 里可以有别的字符，但不许有 #（界面上有分开的输入框兜底）。"""
    if "#" not in text:
        return text, None
    head, _, tail = text.partition("#")
    return head, tail


@dataclass(frozen=True)
class Parts:
    """extract() 的结果。抠不出来的字段是 None，problem 能说清哪一段不对就说。"""

    host: str | None = None
    port: int | None = None
    token: str | None = None
    problem: str | None = None


def extract(text: str) -> Parts:
    """从一段粘贴文本里尽量抠出 host / port / token。**永远不抛异常。**"""
    if not text or not text.strip():
        return Parts(problem="尚未粘贴任何内容")

    # 整条命令行的形式（也可能夹在一整段说明里），优先级最高：
    # 它是唯一能确定"哪儿是地址、哪儿是密钥"的写法。
    server_m = _SERVER_ARG_RE.search(text)
    token_m = _TOKEN_ARG_RE.search(text)
    if server_m or token_m:
        raw = server_m.group(1) if server_m else ""
        token = _unquote(token_m.group(1)) if token_m else None
        return _finish(raw, token)

    # 否则按"第一行非空内容"当一个连接串
    line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    return _finish(line, None)


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _finish(raw: str, explicit_token: str | None) -> Parts:
    body, frag_token = _strip_fragment(raw or "")
    body = _SCHEME_RE.sub("", body.strip())

    # 去掉尾巴上的 /agent 或 /，再拆 host:port
    for suffix in _TRAILING_PATHS:
        if body.endswith(suffix):
            body = body[: -len(suffix)]
            break
    body = body.rstrip("/")

    host, port_text = _split_host_port(body)

    token = explicit_token if explicit_token is not None else frag_token
    if token is not None:
        token = _unquote(token)

    if port_text is None or port_text == "":
        port = DEFAULT_PORT
    elif port_text.isdigit():
        port = int(port_text)
    else:
        return Parts(host=host, port=None, token=token,
                     problem=f"端口位置的 {port_text!r} 不是有效数字")

    return Parts(host=host, port=port, token=token)


@dataclass(frozen=True)
class ConnInfo:
    host: str
    port: int = DEFAULT_PORT
    token: str = ""

    def __post_init__(self) -> None:
        host = (self.host or "").strip()
        if not host:
            raise ConnInfoError("未填写服务器地址。请从服务器端复制完整连接串后粘贴。")
        object.__setattr__(self, "host", host)

        try:
            port = int(self.port)
        except (TypeError, ValueError):
            raise ConnInfoError(f"端口 {self.port!r} 不是有效数字") from None
        if not 1 <= port <= 65535:
            raise ConnInfoError(f"端口 {port} 超出 1–65535 的范围")
        object.__setattr__(self, "port", port)

        token = (self.token or "").strip()
        if not token:
            raise ConnInfoError(
                "未填写密钥。连接串格式应为 ws://地址:端口#密钥，"
                "请从服务器端复制完整连接串后粘贴。")
        object.__setattr__(self, "token", token)

    # -- 各种形式 -----------------------------------------------------------
    @property
    def ws_url(self) -> str:
        """给 agent 连的地址。

        **绝不能带 fragment**：websockets 的 parse_uri 会直接拒绝 # 后面的部分
        （InvalidURI: fragment identifier is meaningless）。所以带 # 的那一串
        只许出现在界面和剪贴板上，流到这里之前 token 必须已经拆出去了。
        """
        return f"ws://{self.host}:{self.port}"

    @property
    def http_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def encode(self) -> str:
        """界面上显示 / 给人复制的那一串。"""
        return f"{self.ws_url}#{self.token}"

    def command_line(self, mode: str = "confirm") -> str:
        """不想用界面时，命令行 agent 该怎么写。"""
        return f'--server {self.ws_url} --token "{self.token}" --mode {mode}'


def parse(text: str) -> ConnInfo:
    """把粘贴进来的东西变成一个能用的 ConnInfo。不够用就抛 ConnInfoError。"""
    parts = extract(text)
    if parts.problem:
        raise ConnInfoError(parts.problem)
    if not parts.host:
        raise ConnInfoError(
            "无法识别服务器地址。正确格式为 ws://192.168.1.23:8765#密钥，"
            "或完整的 --server ... --token ... --mode confirm 命令。")
    return ConnInfo(host=parts.host, port=parts.port or DEFAULT_PORT,
                    token=parts.token or "")

"""消息的校验和排版。纯函数，不碰 Qt——测试和命令行都能直接用。

长度上限只有一份，在 `server/protocol.py` 里：两端的判断必须一致，
否则会出现"这边说发出去了、那边一声不响地丢掉"。
"""

from __future__ import annotations

import time

from server.protocol import MAX_CHAT_CHARS

__all__ = ["MAX_CHAT_CHARS", "normalize", "format_line"]


def normalize(text: str) -> str | None:
    """去掉首尾空白。非字符串、空消息或超长返回 None（调用方据此不发）。"""
    if not isinstance(text, str):
        return None
    text = text.strip()
    if not text or len(text) > MAX_CHAT_CHARS:
        return None
    return text


def format_line(who: str, text: str, ts: float | None = None) -> str:
    """排成 ``[HH:MM:SS] 谁：正文``。

    多行消息的续行按前缀宽度缩进，否则第二行会顶到左边，看着像另一个人说的。
    """
    when = time.localtime(time.time() if ts is None else ts)
    prefix = f"[{time.strftime('%H:%M:%S', when)}] {who}："
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    indent = " " * len(prefix)
    return "\n".join([prefix + lines[0]] + [indent + line for line in lines[1:]])

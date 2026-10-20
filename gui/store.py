"""记住连接信息，省得每次重新粘贴。

放 `%LOCALAPPDATA%\\remote-pc\\client.json`：**用户目录而不是 exe 旁边**。
同学的 exe 可能在 U 盘上，而这个文件里有密钥。

写进去的时机是"握手成功之后"：密钥打错永远不会被记住，
顺带白送一个"这一串确实连上过"的校验。

读的时候**绝不改文件**。损坏的文件留给 save() 去改名留档——
一个 read 函数偷偷动人家的文件，是最难查的那种意外。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from . import paths
from .conninfo import ConnInfo, ConnInfoError

SAVE_VERSION = 1
CLIENT_FILE = "client.json"
CORRUPT_SUFFIX = ".corrupt"


@dataclass(frozen=True)
class Stored:
    conn: ConnInfo
    mode: str = "confirm"


def default_path(*, environ=None, data=None) -> Path:
    base = Path(data) if data is not None else paths.data_dir(environ=environ)
    return base / CLIENT_FILE


def load(*, path: str | Path | None = None) -> Stored | None:
    """读回上次的连接信息。

    文件不在、读不动、不是 JSON、版本对不上、字段不全 —— 一律返回 None。
    **不删文件、不改文件**：里面可能有我们还不认识的东西，留给人自己看。
    """
    target = Path(path) if path is not None else default_path()
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("version") != SAVE_VERSION:
        return None

    try:
        conn = ConnInfo(host=str(data.get("host") or ""),
                        port=data.get("port", 8765),
                        token=str(data.get("token") or ""))
    except ConnInfoError:
        return None

    mode = str(data.get("mode") or "confirm")
    return Stored(conn=conn, mode=mode)


def save(conn: ConnInfo, mode: str = "confirm", *, path: str | Path | None = None) -> Path:
    """把连接信息写下来。

    现有文件读不出来（损坏、或者版本不认识）时先改名成 `.corrupt` 留档，
    而不是直接盖掉——万一那是别人手写的东西呢。
    """
    target = Path(path) if path is not None else default_path()
    target.parent.mkdir(parents=True, exist_ok=True)

    if target.exists() and load(path=target) is None:
        try:
            target.replace(target.with_name(target.name + CORRUPT_SUFFIX))
        except OSError:
            pass

    payload = {
        "version": SAVE_VERSION,
        "host": conn.host,
        "port": conn.port,
        "token": conn.token,
        "mode": mode,
    }
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    return target


def clear(*, path: str | Path | None = None) -> None:
    """忘掉记住的连接信息（密钥也跟着没了）。"""
    target = Path(path) if path is not None else default_path()
    try:
        target.unlink()
    except OSError:
        pass

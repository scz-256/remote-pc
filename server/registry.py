"""连接与会话注册表。

一个 agent（对方的电脑）一条 WebSocket；一个 agent 下可以有多个 session（多条独立 shell），
这样长任务（编译、装依赖）可以跑在 build session 里，同时还能在 main 里干别的。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .protocol import MAX_SESSION_BUFFER_CHARS


@dataclass
class Chunk:
    seq: int
    stream: str      # stdout / stderr / stdin（回显）/ note（系统提示）
    text: str


class Session:
    """一条远程 shell。持有工作目录、输出缓冲、当前进程状态。"""

    def __init__(self, name: str, agent_id: str) -> None:
        self.name = name
        self.agent_id = agent_id
        self.cwd: str | None = None
        self.shell: str | None = None

        self._chunks: deque[Chunk] = deque()
        self._chars = 0
        self._next_seq = 0

        self.running = False
        self.cmd_id: str | None = None
        self.cmd: str | None = None
        self.exit_code: int | None = None
        self.denied: str | None = None
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.supports_stdin = False

        # 本次命令的第一条输出落在哪个序号上。会话是复用的，不记这个的话
        # 每次返回都会把前面几条命令的输出一起带出来。
        self.start_seq = 0

        self.done = asyncio.Event()

    # -- 输出缓冲 ---------------------------------------------------------
    def append(self, stream: str, text: str) -> int:
        seq = self._next_seq
        self._next_seq += 1
        self._chunks.append(Chunk(seq, stream, text))
        self._chars += len(text)
        # 超上限就从头部丢，保证内存不会因为一个 while(1) 的进程爆掉
        while self._chars > MAX_SESSION_BUFFER_CHARS and len(self._chunks) > 1:
            dropped = self._chunks.popleft()
            self._chars -= len(dropped.text)
        return seq

    def text(self, since_seq: int = 0, streams: tuple[str, ...] | None = None) -> str:
        parts = []
        for chunk in self._chunks:
            if chunk.seq < since_seq:
                continue
            if streams and chunk.stream not in streams:
                continue
            parts.append(chunk.text)
        return "".join(parts)

    @property
    def last_seq(self) -> int:
        return self._next_seq

    # -- 生命周期 ---------------------------------------------------------
    def begin(self, cmd_id: str, cmd: str, shell: str | None) -> None:
        self.cmd_id = cmd_id
        self.cmd = cmd
        self.shell = shell
        self.running = True
        self.exit_code = None
        self.denied = None
        self.started_at = time.time()
        self.finished_at = None
        # agent 侧始终把子进程的 stdin 留着，所以跑的时候可以随时喂输入
        self.supports_stdin = True
        self.start_seq = self._next_seq
        self.done.clear()

    def finish(self, exit_code: int | None, cwd: str | None = None) -> None:
        self.running = False
        self.exit_code = exit_code
        self.finished_at = time.time()
        self.supports_stdin = False
        if cwd:
            self.cwd = cwd
        self.done.set()

    def result(self) -> dict[str, Any]:
        """只含本次命令的输出——给 exec 的返回值用。"""
        return self.to_dict(self.start_seq)

    def to_dict(self, since_seq: int = 0) -> dict[str, Any]:
        return {
            "session": self.name,
            "agent": self.agent_id,
            "cwd": self.cwd,
            "shell": self.shell,
            "running": self.running,
            "cmd": self.cmd,
            "exit_code": self.exit_code,
            "denied": self.denied,
            "supports_stdin": self.supports_stdin,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "last_seq": self.last_seq,
            "stdout": self.text(since_seq, ("stdout",)),
            "stderr": self.text(since_seq, ("stderr",)),
            "notes": self.text(since_seq, ("note",)),
        }


class Transfer:
    """一次文件上传或下载。"""

    def __init__(self, transfer_id: str, direction: str, path: str, size: int = 0) -> None:
        self.id = transfer_id
        self.direction = direction      # put / get
        self.path = path
        self.size = size
        self.data = bytearray()
        self.sha256_expected: str | None = None
        self.sha256_actual: str | None = None
        self.ok: bool | None = None
        self.message: str = ""
        self.finished = asyncio.Event()


class AgentConn:
    """一条 agent WebSocket 连接。"""

    def __init__(self, agent_id: str, ws: Any) -> None:
        self.agent_id = agent_id
        self.ws = ws
        self.info: dict[str, Any] = {}
        # agent 在 auth 帧里自报的界面类型（gui / console）。旧 agent 不带这个键，
        # 一律按 console 处理——那种 agent 没有地方显示消息。
        self.ui: str = "console"
        self.session_code: str = ""
        self.connected_at = time.time()
        self.sessions: dict[str, Session] = {}
        self.transfers: dict[str, Transfer] = {}
        self.last_pong = time.time()
        self._send_lock = asyncio.Lock()

    async def send(self, msg: dict[str, Any]) -> None:
        # 多个 API 请求可能同时发指令，锁一下避免帧交错
        async with self._send_lock:
            await self.ws.send_text(json.dumps(msg, ensure_ascii=False))

    def session(self, name: str) -> Session:
        sess = self.sessions.get(name)
        if sess is None:
            sess = Session(name, self.agent_id)
            self.sessions[name] = sess
        return sess

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent_id,
            "session_code": self.session_code,
            "connected_at": self.connected_at,
            "age_s": round(time.time() - self.connected_at, 1),
            "ui": self.ui,
            "info": self.info,
            "sessions": [s.to_dict(0) | {"stdout": "", "stderr": "", "notes": ""}
                         for s in self.sessions.values()],
        }


class Registry:
    """所有 agent 连接的集合。"""

    def __init__(self) -> None:
        self._agents: dict[str, AgentConn] = {}
        self._lock = asyncio.Lock()

    async def add(self, conn: AgentConn) -> AgentConn | None:
        """注册新连接。同一 agent_id 再来一条时，返回被顶掉的旧连接（调用方负责关掉）。"""
        async with self._lock:
            old = self._agents.get(conn.agent_id)
            self._agents[conn.agent_id] = conn
        return old

    async def remove(self, conn: AgentConn) -> None:
        async with self._lock:
            # 只有当注册表里还是这条连接时才删，避免把新连接误删
            if self._agents.get(conn.agent_id) is conn:
                del self._agents[conn.agent_id]

    def get(self, name: str | None = None) -> AgentConn:
        if not self._agents:
            raise LookupError("没有 agent 在线")
        if name in (None, "", "default"):
            if len(self._agents) == 1:
                return next(iter(self._agents.values()))
            names = ", ".join(sorted(self._agents))
            raise LookupError(f"有多个 agent 在线，需要指定 agent：{names}")
        conn = self._agents.get(name)
        if conn is None:
            names = ", ".join(sorted(self._agents)) or "（无）"
            raise LookupError(f"没有名为 {name!r} 的 agent，在线的是：{names}")
        return conn

    def all(self) -> list[AgentConn]:
        return list(self._agents.values())

    @property
    def online(self) -> bool:
        return bool(self._agents)


registry = Registry()

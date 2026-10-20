"""中枢：agent 从这里接入，本地 CLI / MCP server 从这里下发指令。

两条通道：
  ws://0.0.0.0:8765/agent  —— 对方的 agent 主动连进来（反向连接，他那边不开监听端口）
  http://127.0.0.1:8765/api/* —— 本机 CLI 和 MCP server 走这条，需要同一个 token

反向连接是刻意的：对方的机器上不需要开防火墙、不需要端口转发，关掉窗口就立刻失联。
"""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import logging
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Callable

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from . import policy, protocol
from .audit import AuditLog, sha256_text
from .config import ServerConfig, load_config
from .registry import AgentConn, Registry, Session, Transfer

log = logging.getLogger("hub")

config: ServerConfig = load_config()
registry = Registry()
audit = AuditLog()

# ---------------------------------------------------------------------------
# 消息监听器
#
# 服务器端页面和 hub 跑在同一个进程里，但 HTTP 是请求/响应，没法把客户端主动发来的
# 消息推给页面。所以这里留一组进程内回调：hub 收到 T_CHAT 就依次通知它们。
# 回调必须在图形界面线程里只做 emit 的动作，不要阻塞——它在 hub 的事件循环里执行。
# 消息正文刻意不写 audit、不写 log：这只是两个人之间的对话，不是操作记录。
# ---------------------------------------------------------------------------
ChatListener = Callable[[str, str, float], None]

_chat_listeners: list[ChatListener] = []
_chat_listeners_lock = threading.Lock()


def add_chat_listener(fn: ChatListener) -> None:
    with _chat_listeners_lock:
        if fn not in _chat_listeners:
            _chat_listeners.append(fn)


def remove_chat_listener(fn: ChatListener) -> None:
    with _chat_listeners_lock:
        try:
            _chat_listeners.remove(fn)
        except ValueError:
            pass


def chat_available() -> bool:
    """服务器端有没有人在看消息面板。没有的话告诉客户端"不支持"，免得消息石沉大海。"""
    with _chat_listeners_lock:
        return bool(_chat_listeners)


def _notify_chat(agent_id: str, text: str, ts: float) -> None:
    with _chat_listeners_lock:
        listeners = list(_chat_listeners)
    for fn in listeners:
        try:
            fn(agent_id, text, ts)
        except Exception:  # 界面侧出错不能影响 hub 收别的帧
            log.exception("消息监听器处理失败")

app = FastAPI(title="remote-pc hub", version=str(protocol.PROTO_VERSION))


# ---------------------------------------------------------------------------
# 鉴权
# ---------------------------------------------------------------------------
async def require_token(authorization: str | None = Header(default=None)) -> None:
    """本地 API 也要 token。

    localhost 不是安全边界——同机器上的其他用户/进程一样能访问，
    而且这个 API 能让人在别人电脑上执行任意命令，值得多这一道。
    """
    if not config.token:
        raise HTTPException(500, "服务端未配置 token，请先运行 tools/gen_token.py")
    expected = f"Bearer {config.token}"
    if not authorization or not hmac.compare_digest(authorization, expected):
        raise HTTPException(401, "token 无效")


def _client_allowed(host: str | None) -> bool:
    if not host:
        return False
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    for net in config.allowed_networks:
        try:
            if addr in ipaddress.ip_network(net, strict=False):
                return True
        except ValueError:
            log.warning("配置里的网段 %r 无法解析，已忽略", net)
    return False


# ---------------------------------------------------------------------------
# agent 接入
# ---------------------------------------------------------------------------
@app.websocket("/agent")
async def agent_endpoint(ws: WebSocket) -> None:
    peer = ws.client.host if ws.client else None

    if not _client_allowed(peer):
        log.warning("拒绝来自 %s 的连接：不在允许的网段内", peer)
        await ws.close(code=protocol.CLOSE_NETWORK_DENIED)
        return

    await ws.accept()

    nonce = secrets.token_hex(32)
    await ws.send_text(json.dumps({"t": protocol.T_CHALLENGE, "nonce": nonce,
                                   "ver": protocol.PROTO_VERSION}))

    try:
        raw = await asyncio.wait_for(ws.receive_text(), timeout=protocol.AUTH_TIMEOUT_S)
        msg = json.loads(raw)
    except (asyncio.TimeoutError, json.JSONDecodeError, WebSocketDisconnect):
        log.warning("%s 握手超时或格式错误", peer)
        await ws.close(code=protocol.CLOSE_BAD_AUTH)
        return

    if msg.get("t") != protocol.T_AUTH:
        await ws.close(code=protocol.CLOSE_BAD_AUTH)
        return

    if msg.get("ver") != protocol.PROTO_VERSION:
        await ws.send_text(json.dumps({"t": protocol.T_ERROR,
                                       "msg": f"协议版本不匹配：agent={msg.get('ver')} "
                                              f"hub={protocol.PROTO_VERSION}"}))
        await ws.close(code=protocol.CLOSE_BAD_AUTH)
        return

    expected = protocol.hmac_proof(config.token, nonce)
    if not config.token or not hmac.compare_digest(msg.get("proof", ""), expected):
        log.warning("%s 鉴权失败", peer)
        audit.write("auth_fail", peer=peer)
        await ws.close(code=protocol.CLOSE_BAD_AUTH)
        return

    agent_id = str(msg.get("agent_id") or f"agent-{secrets.token_hex(3)}")
    conn = AgentConn(agent_id, ws)
    conn.info = msg.get("host", {}) or {}
    conn.info["peer"] = peer
    # 旧 agent 不带这个键，按命令行版处理
    conn.ui = protocol.UI_GUI if msg.get("ui") == protocol.UI_GUI else protocol.UI_CONSOLE

    # 6 位会话码：不是鉴权手段，是给两边的人核对用的——
    # 让对方念一下屏幕上的码，确认连的确实是本人而不是谁偷了 token。
    conn.session_code = f"{secrets.randbelow(1000000):06d}"

    old = await registry.add(conn)
    if old is not None:
        log.info("agent %s 重新连接，已替换旧连接", agent_id)
        try:
            await old.ws.close(code=protocol.CLOSE_SUPERSEDED)
        except Exception:
            pass

    await conn.send({"t": protocol.T_READY, "agent_id": agent_id,
                     "hub_ver": protocol.PROTO_VERSION,
                     "session_code": conn.session_code,
                     # 只有服务器端真的开着消息面板时才为真。客户端据此决定
                     # 要不要把输入框点亮，而不是发出去之后无声无息。
                     "chat": chat_available()})
    log.info("agent %s 已接入 %s（会话码 %s）", agent_id, peer, conn.session_code)
    audit.write("agent_connect", agent=agent_id, peer=peer,
                session_code=conn.session_code, host=conn.info)

    try:
        while True:
            raw = await ws.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                log.warning("agent %s 发来无法解析的帧，长度 %d", agent_id, len(raw))
                continue
            await _dispatch(conn, data)
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # 网络异常不该把整个 hub 带下去
        log.warning("agent %s 连接异常：%r", agent_id, exc)
    finally:
        await registry.remove(conn)
        _fail_running_sessions(conn)
        log.info("agent %s 已断开", agent_id)
        audit.write("agent_disconnect", agent=agent_id, peer=peer)


# ws://IP:8765 和 ws://IP:8765/agent 都接受——少一个让人踩的坑
app.add_api_websocket_route("/", agent_endpoint)


def _fail_running_sessions(conn: AgentConn) -> None:
    for sess in conn.sessions.values():
        if sess.running:
            sess.append("note", "\n[连接断开，该命令的结果已丢失]\n")
            sess.finish(None)


async def _dispatch(conn: AgentConn, msg: dict[str, Any]) -> None:
    kind = msg.get("t")
    sid = msg.get("session") or "main"

    if kind == protocol.T_CHUNK:
        sess = conn.session(sid)
        sess.append(msg.get("stream", "stdout"), msg.get("data", ""))
        return

    if kind == protocol.T_DONE:
        sess = conn.session(sid)
        if msg.get("denied"):
            sess.denied = msg["denied"]
            sess.append("note", f"\n[已拒绝：{msg['denied']}]\n")
        sess.finish(msg.get("exit"), msg.get("cwd"))
        audit.write("exec_done", agent=conn.agent_id, session=sid,
                    cmd=sess.cmd, exit=msg.get("exit"), cwd=msg.get("cwd"),
                    denied=msg.get("denied"),
                    out_sha=sha256_text(sess.text(sess.start_seq,
                                                  ("stdout", "stderr"))))
        return

    if kind == protocol.T_DENIED:
        sess = conn.session(sid)
        sess.denied = msg.get("reason", "被策略拒绝")
        sess.append("note", f"\n[已拒绝：{sess.denied}]\n")
        sess.finish(None)
        audit.write("exec_denied", agent=conn.agent_id, session=sid,
                    cmd=sess.cmd, reason=sess.denied, pattern=msg.get("pattern"))
        return

    if kind == protocol.T_ERROR:
        sess = conn.session(sid)
        sess.append("note", f"\n[agent 报错：{msg.get('msg')}]\n")
        sess.finish(None)
        return

    if kind == protocol.T_FILE_BEGIN:
        tr = conn.transfers.get(msg.get("transfer_id", ""))
        if tr is not None:
            tr.size = int(msg.get("size", 0))
            tr.sha256_expected = msg.get("sha256")
        return

    if kind in (protocol.T_FILE_CHUNK, protocol.T_PUT):
        tr = conn.transfers.get(msg.get("transfer_id", ""))
        if tr is not None:
            import base64
            tr.data += base64.b64decode(msg.get("data", ""))
        return

    if kind == protocol.T_FILE_DONE:
        tr = conn.transfers.get(msg.get("transfer_id", ""))
        if tr is not None:
            tr.ok = bool(msg.get("ok", True))
            tr.message = msg.get("msg", "")
            tr.sha256_actual = msg.get("sha256")
            tr.finished.set()
        return

    if kind == protocol.T_SHOT_DATA:
        tr = conn.transfers.get(msg.get("transfer_id", ""))
        if tr is not None:
            import base64
            tr.data = bytearray(base64.b64decode(msg.get("data", "")))
            tr.ok = True
            tr.message = msg.get("msg", "")
            tr.finished.set()
        return

    if kind == protocol.T_CLIP_DATA:
        tr = conn.transfers.get(msg.get("transfer_id", ""))
        if tr is not None:
            tr.data = bytearray(msg.get("text", "").encode("utf-8"))
            tr.ok = True
            tr.message = msg.get("msg", "")
            tr.finished.set()
        return

    if kind == protocol.T_PONG:
        conn.last_pong = time.time()
        return

    if kind == protocol.T_CHAT:
        text = msg.get("text")
        if not isinstance(text, str):
            return
        text = text.strip()
        # 空消息、超长消息、命令行 agent 发来的消息直接丢。
        # 正文不进日志也不进审计——这是两个人之间的对话。
        if not text or len(text) > protocol.MAX_CHAT_CHARS:
            return
        if getattr(conn, "ui", protocol.UI_CONSOLE) != protocol.UI_GUI:
            return
        ts = msg.get("ts")
        _notify_chat(conn.agent_id, text,
                     ts if isinstance(ts, (int, float)) else time.time())
        return

    log.debug("agent %s 发来未处理的消息类型 %r", conn.agent_id, kind)


# ---------------------------------------------------------------------------
# 本地 API
# ---------------------------------------------------------------------------
class ExecRequest(BaseModel):
    cmd: str
    agent: str | None = None
    session: str = "main"
    cwd: str | None = None
    shell: str | None = None
    timeout_s: float | None = None
    wait: bool = True


class SessionRequest(BaseModel):
    agent: str | None = None
    session: str = "main"


class StdinRequest(SessionRequest):
    data: str


class SignalRequest(SessionRequest):
    sig: str = "ctrl-c"


class PutRequest(BaseModel):
    local_path: str
    remote_path: str
    agent: str | None = None
    mode: str = "overwrite"


class GetRequest(BaseModel):
    remote_path: str
    local_path: str
    agent: str | None = None
    max_bytes: int = protocol.MAX_TRANSFER_BYTES


class ShotRequest(SessionRequest):
    monitor: int = 1
    save_to: str | None = None


class ClipSetRequest(SessionRequest):
    text: str


class ChatRequest(BaseModel):
    text: str
    agent: str | None = None


def _resolve(agent: str | None) -> AgentConn:
    try:
        return registry.get(agent)
    except LookupError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/status", dependencies=[Depends(require_token)])
async def api_status() -> dict[str, Any]:
    return {
        "hub": "ok",
        "mode": config.mode,
        "proto_version": protocol.PROTO_VERSION,
        # 客户端拿它来回答"我会不会被 4403 拒掉"：这条连接**还没有**发生，
        # 所以只能靠对比自己到 hub 的出口地址和这份网段清单来预判。
        # 放在这个端点里是安全的——它本来就在 require_token 后面，
        # 读得到它的人已经拿得到这台机器的执行权了。
        "allowed_networks": list(config.allowed_networks),
        "agents": [c.to_dict() for c in registry.all()],
    }


@app.post("/api/chat", dependencies=[Depends(require_token)])
async def api_chat(req: ChatRequest) -> dict[str, Any]:
    """把服务器端的一句话发给客户端。对端必须也是图形界面。"""
    conn = _resolve(req.agent)
    if conn.ui != protocol.UI_GUI:
        raise HTTPException(409, "对端为命令行 agent，不支持消息。")
    if not chat_available():
        raise HTTPException(409, "服务器端未开启消息面板，消息无处显示。")

    text = (req.text or "").strip()
    if not text:
        raise HTTPException(400, "消息内容为空。")
    if len(text) > protocol.MAX_CHAT_CHARS:
        raise HTTPException(400, f"消息过长（上限 {protocol.MAX_CHAT_CHARS} 字符）。")

    await conn.send({"t": protocol.T_CHAT_SERVER, "text": text, "ts": time.time()})
    return {"ok": True, "agent": conn.agent_id}


@app.post("/api/exec", dependencies=[Depends(require_token)])
async def api_exec(req: ExecRequest) -> dict[str, Any]:
    conn = _resolve(req.agent)
    sess = conn.session(req.session)

    if sess.running:
        raise HTTPException(
            409,
            f"session {req.session!r} 正忙（正在执行：{sess.cmd!r}）。"
            f"请更换 session，或先执行 kill。",
        )

    # 服务端这一遍拦截：agent 侧也会拦，但风险主要来自 LLM 手滑，
    # 不能只靠一层。
    decision = policy.check_command(req.cmd, config.mode)
    audit.write("exec_request", agent=conn.agent_id, session=req.session,
                cmd=req.cmd, cwd=req.cwd, decision=decision.action, reason=decision.label)
    if decision.action == policy.DENY:
        raise HTTPException(403, f"本地策略拒绝：{decision.label}（命中：{decision.pattern}）")

    cmd_id = protocol.new_id()
    sess.begin(cmd_id, req.cmd, req.shell)

    await conn.send({
        "t": protocol.T_EXEC, "id": cmd_id, "session": req.session,
        "cmd": req.cmd, "cwd": req.cwd, "shell": req.shell,
    })

    timeout = req.timeout_s if req.timeout_s is not None else config.exec_default_timeout_s
    if not req.wait:
        return {"session": req.session, "running": True, "agent": conn.agent_id}

    return await _await_session(sess, timeout)


async def _await_session(sess: Session, timeout: float) -> dict[str, Any]:
    try:
        await asyncio.wait_for(sess.done.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        out = sess.result()
        out["timed_out"] = True
        out["note"] = f"命令在 {timeout} 秒内未结束，仍在后台运行。可继续写入 stdin 或执行 kill。"
        return out
    return sess.result()


@app.get("/api/sessions", dependencies=[Depends(require_token)])
async def api_sessions(agent: str | None = None, session: str | None = None,
                       since_seq: int = 0) -> dict[str, Any]:
    conn = _resolve(agent)
    if session is None:
        return {"agent": conn.agent_id,
                "sessions": [s.to_dict(since_seq) for s in conn.sessions.values()]}
    sess = conn.sessions.get(session)
    if sess is None:
        raise HTTPException(404, f"没有这个 session：{session}")
    return sess.to_dict(since_seq)


@app.post("/api/stdin", dependencies=[Depends(require_token)])
async def api_stdin(req: StdinRequest) -> dict[str, Any]:
    conn = _resolve(req.agent)
    sess = conn.sessions.get(req.session)
    if sess is None or not sess.running:
        raise HTTPException(409, f"session {req.session!r} 当前没有在运行的进程")
    if not sess.supports_stdin:
        raise HTTPException(409, "该命令的标准输入已关闭，无法写入")
    await conn.send({"t": protocol.T_STDIN, "session": req.session, "data": req.data})
    return {"ok": True}


@app.post("/api/signal", dependencies=[Depends(require_token)])
async def api_signal(req: SignalRequest) -> dict[str, Any]:
    conn = _resolve(req.agent)
    sess = conn.sessions.get(req.session)
    if sess is None or not sess.running:
        raise HTTPException(409, f"session {req.session!r} 当前没有在运行的进程")
    await conn.send({"t": protocol.T_SIGNAL, "session": req.session,
                     "id": sess.cmd_id, "sig": req.sig})
    audit.write("signal", agent=conn.agent_id, session=req.session, sig=req.sig,
                cmd=sess.cmd)
    return {"ok": True}


@app.post("/api/kill", dependencies=[Depends(require_token)])
async def api_kill(req: SessionRequest) -> dict[str, Any]:
    conn = _resolve(req.agent)
    sess = conn.sessions.get(req.session)
    if sess is None or not sess.running:
        raise HTTPException(409, f"session {req.session!r} 当前没有在运行的进程")
    await conn.send({"t": protocol.T_SIGNAL, "session": req.session,
                     "id": sess.cmd_id, "sig": "kill"})
    audit.write("kill", agent=conn.agent_id, session=req.session, cmd=sess.cmd)
    return {"ok": True}


# -- 文件 -------------------------------------------------------------------
def _new_transfer(conn: AgentConn, direction: str, path: str) -> Transfer:
    tid = protocol.new_id()
    tr = Transfer(tid, direction, path)
    conn.transfers[tid] = tr
    return tr


@app.post("/api/files/put", dependencies=[Depends(require_token)])
async def api_put(req: PutRequest) -> dict[str, Any]:
    import base64
    import hashlib

    conn = _resolve(req.agent)
    local = Path(req.local_path)
    if not local.is_file():
        raise HTTPException(400, f"本地文件不存在：{local}")
    size = local.stat().st_size
    if size > protocol.MAX_TRANSFER_BYTES:
        raise HTTPException(400, f"文件太大：{size} 字节 > 上限 {protocol.MAX_TRANSFER_BYTES}")

    blob = local.read_bytes()
    digest = hashlib.sha256(blob).hexdigest()

    tr = _new_transfer(conn, "put", req.remote_path)
    await conn.send({"t": protocol.T_PUT_BEGIN, "transfer_id": tr.id,
                     "path": req.remote_path, "size": size, "sha256": digest})

    step = protocol.FILE_CHUNK_SIZE
    for seq, off in enumerate(range(0, size, step)):
        piece = blob[off:off + step]
        await conn.send({"t": protocol.T_PUT, "transfer_id": tr.id, "seq": seq,
                         "data": base64.b64encode(piece).decode("ascii")})

    await conn.send({"t": protocol.T_PUT_END, "transfer_id": tr.id})
    audit.write("file_put", agent=conn.agent_id, local=str(local),
                remote=req.remote_path, size=size, sha256=digest)

    try:
        await asyncio.wait_for(tr.finished.wait(), timeout=300)
    except asyncio.TimeoutError:
        raise HTTPException(504, "上传超时，对端无响应")
    finally:
        conn.transfers.pop(tr.id, None)

    if not tr.ok:
        raise HTTPException(500, f"上传失败：{tr.message}")
    return {"ok": True, "remote_path": req.remote_path, "size": size, "sha256": digest}


@app.post("/api/files/get", dependencies=[Depends(require_token)])
async def api_get(req: GetRequest) -> dict[str, Any]:
    conn = _resolve(req.agent)
    tr = _new_transfer(conn, "get", req.remote_path)
    await conn.send({"t": protocol.T_GET, "transfer_id": tr.id,
                     "path": req.remote_path, "max_bytes": req.max_bytes})
    try:
        await asyncio.wait_for(tr.finished.wait(), timeout=300)
    except asyncio.TimeoutError:
        raise HTTPException(504, "下载超时，对端无响应")
    finally:
        conn.transfers.pop(tr.id, None)

    if not tr.ok:
        raise HTTPException(404, f"取文件失败：{tr.message}")

    if tr.sha256_expected:
        import hashlib
        actual = hashlib.sha256(bytes(tr.data)).hexdigest()
        if actual != tr.sha256_expected:
            raise HTTPException(500, f"校验和不匹配，文件可能在传输中损坏"
                                     f"（期望 {tr.sha256_expected[:12]}，实际 {actual[:12]}）")

    dest = Path(req.local_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(bytes(tr.data))
    audit.write("file_get", agent=conn.agent_id, remote=req.remote_path,
                local=str(dest), size=len(tr.data))
    return {"ok": True, "local_path": str(dest), "size": len(tr.data)}


# -- 屏幕 / 剪贴板 ----------------------------------------------------------
@app.post("/api/screen/shot", dependencies=[Depends(require_token)])
async def api_shot(req: ShotRequest) -> dict[str, Any]:
    conn = _resolve(req.agent)
    tr = _new_transfer(conn, "shot", "screen")
    await conn.send({"t": protocol.T_SHOT, "transfer_id": tr.id,
                     "session": req.session, "monitor": req.monitor})
    try:
        await asyncio.wait_for(tr.finished.wait(), timeout=60)
    except asyncio.TimeoutError:
        raise HTTPException(504, "截屏超时")
    finally:
        conn.transfers.pop(tr.id, None)

    if not tr.ok:
        raise HTTPException(500, f"截屏失败：{tr.message}")

    from datetime import datetime
    from .config import LOG_DIR
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    dest = Path(req.save_to) if req.save_to else (
        LOG_DIR / f"screen-{datetime.now().strftime('%Y%m%d-%H%M%S')}.png"
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(bytes(tr.data))
    audit.write("screenshot", agent=conn.agent_id, saved=str(dest), bytes=len(tr.data))
    return {"ok": True, "local_path": str(dest), "size": len(tr.data)}


@app.post("/api/clipboard/get", dependencies=[Depends(require_token)])
async def api_clip_get(req: SessionRequest) -> dict[str, Any]:
    conn = _resolve(req.agent)
    tr = _new_transfer(conn, "clip", "clipboard")
    await conn.send({"t": protocol.T_CLIP_GET, "transfer_id": tr.id})
    try:
        await asyncio.wait_for(tr.finished.wait(), timeout=30)
    except asyncio.TimeoutError:
        raise HTTPException(504, "读剪贴板超时")
    finally:
        conn.transfers.pop(tr.id, None)
    if not tr.ok:
        raise HTTPException(500, f"读剪贴板失败：{tr.message}")
    return {"ok": True, "text": tr.data.decode("utf-8", errors="replace")}


@app.post("/api/clipboard/set", dependencies=[Depends(require_token)])
async def api_clip_set(req: ClipSetRequest) -> dict[str, Any]:
    conn = _resolve(req.agent)
    tr = _new_transfer(conn, "clip", "clipboard")
    await conn.send({"t": protocol.T_CLIP_SET, "transfer_id": tr.id, "text": req.text})
    try:
        await asyncio.wait_for(tr.finished.wait(), timeout=30)
    except asyncio.TimeoutError:
        raise HTTPException(504, "写剪贴板超时")
    finally:
        conn.transfers.pop(tr.id, None)
    if not tr.ok:
        raise HTTPException(500, f"写剪贴板失败：{tr.message}")
    return {"ok": True}


@app.post("/api/ping", dependencies=[Depends(require_token)])
async def api_ping(req: SessionRequest) -> dict[str, Any]:
    conn = _resolve(req.agent)
    await conn.send({"t": protocol.T_PING, "ts": time.time()})
    return {"ok": True, "age_s": round(time.time() - conn.connected_at, 1)}


def main() -> None:
    import uvicorn

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    if not config.token:
        raise SystemExit("尚未配置 token，请先运行：python tools/gen_token.py")
    log.info("hub 启动于 %s:%d（模式 %s）", config.host, config.port, config.mode)
    log.info("agent 连接地址：ws://<本机局域网IP>:%d", config.port)
    uvicorn.run(app, host=config.host, port=config.port, log_level="warning")


if __name__ == "__main__":
    main()

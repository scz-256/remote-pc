"""两端共用的线上协议定义。

agent/agent.py 里有一份等价的手抄副本——agent 要保持单文件、零依赖、对方能一眼读完，
所以没有共享模块。改一边就必须同步另一边。
"""

from __future__ import annotations

import uuid

PROTO_VERSION = 1

# --- server -> agent ---
T_CHALLENGE = "challenge"   # 握手：服务端发随机数
T_READY = "ready"           # 握手：鉴权通过
T_EXEC = "exec"             # 起一个进程（也用于同一 session 里的下一条命令）
T_STDIN = "stdin"           # 往正在跑的进程写输入
T_SIGNAL = "signal"         # 中断：ctrl-c / kill
T_PUT_BEGIN = "put_begin"   # 上传：开始
T_PUT = "put"               # 上传：一片
T_PUT_END = "put_end"       # 上传：结束
T_GET = "get"               # 下载：请求
T_SHOT = "shot"             # 截屏
T_CLIP_GET = "clip_get"     # 读剪贴板
T_CLIP_SET = "clip_set"     # 写剪贴板
T_PING = "ping"
T_CHAT_SERVER = "chat_server"  # 服务器端发给客户端的一条消息（仅图形界面）

# --- agent -> server ---
T_AUTH = "auth"             # 握手：HMAC 应答 + 主机信息
T_CHUNK = "chunk"           # 输出增量
T_DONE = "done"             # 命令结束
T_DENIED = "denied"         # 被策略拦下 / 对方点了拒绝
T_FILE_BEGIN = "file_begin" # 下载：开始
T_FILE_CHUNK = "file_chunk" # 下载：一片
T_FILE_DONE = "file_done"   # 下载完成 / 上传结果回执
T_SHOT_DATA = "shot_data"   # 截屏数据
T_CLIP_DATA = "clip_data"   # 剪贴板内容
T_ERROR = "error"
T_PONG = "pong"
T_CHAT = "chat"             # 客户端发给服务器端的一条消息（仅图形界面）

SERVER_TO_AGENT = frozenset({
    T_CHALLENGE, T_READY, T_EXEC, T_STDIN, T_SIGNAL,
    T_PUT_BEGIN, T_PUT, T_PUT_END, T_GET, T_SHOT, T_CLIP_GET, T_CLIP_SET, T_PING,
    T_CHAT_SERVER,
})
AGENT_TO_SERVER = frozenset({
    T_AUTH, T_CHUNK, T_DONE, T_DENIED, T_FILE_BEGIN, T_FILE_CHUNK,
    T_FILE_DONE, T_SHOT_DATA, T_CLIP_DATA, T_ERROR, T_PONG, T_CHAT,
})

# agent 在 auth 帧里自报的界面类型。旧 agent 不带这个键，hub 按 console 处理。
UI_GUI = "gui"
UI_CONSOLE = "console"

# WebSocket 关闭码（4000-4999 是应用保留段）
CLOSE_BAD_AUTH = 4401
CLOSE_SUPERSEDED = 4402        # 同一 agent_id 又连了一条，旧的被顶掉
CLOSE_NETWORK_DENIED = 4403    # 对端地址不在 allowed_networks 内

AUTH_TIMEOUT_S = 10.0
PING_INTERVAL_S = 20.0

# 文件分片：base64 后约 341KB，低于 websockets 客户端默认的 1MB 单帧上限
FILE_CHUNK_SIZE = 256 * 1024
MAX_TRANSFER_BYTES = 512 * 1024 * 1024

# 每个 session 保留的输出上限（字符），超了从头部丢
MAX_SESSION_BUFFER_CHARS = 4 * 1024 * 1024

# 一条消息的字符上限。输入框是单行，这个上限只是防止有人整段粘贴。
MAX_CHAT_CHARS = 2000


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def hmac_proof(token: str, nonce: str) -> str:
    """握手应答：HMAC-SHA256(token, nonce)。token 本身永不上网。

    这样即使有人录下整段握手，也无法重放或反推 token。
    """
    import hashlib
    import hmac

    return hmac.new(
        token.encode("utf-8"), nonce.encode("utf-8"), hashlib.sha256
    ).hexdigest()

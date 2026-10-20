"""本地客户端库：CLI 和 MCP server 都走这里访问 hub。

同步实现——调用方是命令行和 MCP 工具，都是"发一条命令等结果"的节奏，
用同步客户端能让上层代码简单很多。
"""

from __future__ import annotations

import logging
from typing import Any, Callable

import httpx

from .config import ServerConfig, load_config

log = logging.getLogger("remote")


class RemoteError(RuntimeError):
    """远端或 hub 返回了错误。消息已经整理成人能读的形式。"""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        # HTTP 状态码；连不上的时候是 None。调用方靠这个属性分流，
        # 不必去正则匹配消息文本——文本匹配只留给异常链断掉的老路径。
        self.status = status


class RemoteClient:
    def __init__(self, config: ServerConfig | None = None, timeout_s: float = 900.0,
                 agent: str | None = None,
                 reload: Callable[[], ServerConfig] | None = None,
                 transport: httpx.BaseTransport | None = None) -> None:
        # 401 时允不允许自己重读一遍配置（见 _refresh_token）：
        # 配置是自己从磁盘读的、或者调用方明确给了 reload —— 都算可以。
        # 调用方自己拼的配置对象（比如 gui/linkcheck.py 拿输入框现搭的那个）
        # 不该被磁盘上的东西悄悄覆盖，所以默认不重读。
        self._may_reload = config is None or reload is not None
        self.config = config or load_config()
        # 多台 agent 在线时 hub 会拒绝"随便挑一台"，所以在这里钉死一台，
        # 省得每个调用点都写一遍 agent=（也免得漏掉某一处就串台了）。
        self.default_agent = agent
        self._timeout_s = timeout_s
        self._reload = reload or load_config
        self._transport = transport      # 只在测试里给，用来拦掉真网络
        self._http = self._build_http(self.config)

    def _build_http(self, cfg: ServerConfig) -> httpx.Client:
        return httpx.Client(
            base_url=cfg.hub_url,
            headers={"Authorization": f"Bearer {cfg.token}"},
            # 命令可能跑很久，读超时必须给足；连接超时保持短，方便快速发现 hub 没起
            timeout=httpx.Timeout(self._timeout_s, connect=5.0),
            transport=self._transport,
        )

    def _refresh_token(self) -> bool:
        """hub 说 token 无效。先别急着认定"密钥错了"——多半是我们手里这把过期了。

        `__init__` 把密钥抄进了 httpx 的默认请求头，之后再没看过配置文件。
        而长住的进程（`server/mcp_server.py` 把 client 缓存在模块全局里）可以
        带着几小时前那把密钥一直跑：服务器端界面上点一次「随机生成密钥」，
        它就**永久** 401，现象是"CC 忽然说 token 无效"，而且没有线索可追。

        所以这里重读一次配置文件，密钥真变了就换上去、返回 True 让调用方重试。
        这是同一个进程里重读一遍文件——**协议一个字没动**。

        只重试一次：换完再 401 的话，`cfg.token` 和手里这把就一样了，直接返回
        False，不会来回打转。
        """
        if not self._may_reload:
            return False
        try:
            cfg = self._reload()
        except Exception as exc:      # noqa: BLE001 - 读不出来就按原样失败
            log.debug("重读配置失败：%r", exc)
            return False
        if not cfg or cfg.token == self.config.token:
            return False              # 文件里还是那把，那就是真·密钥不对
        log.warning("hub 报告 token 无效，但配置文件中的密钥已变更；正在使用新密钥重试")
        self.config = cfg
        old, self._http = self._http, self._build_http(cfg)
        old.close()
        return True

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "RemoteClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- 内部 ---------------------------------------------------------------
    def _post(self, path: str, payload: dict | None = None, **params) -> dict[str, Any]:
        body = dict(payload or {})
        if self.default_agent and body.get("agent") is None:
            body["agent"] = self.default_agent
        return self._call("POST", path, json=body, params=params or None)

    def _get(self, path: str, **params) -> dict[str, Any]:
        if self.default_agent and params.get("agent") is None:
            params["agent"] = self.default_agent
        return self._call("GET", path, params=params or None)

    def _request(self, method: str, path: str, **kw) -> httpx.Response:
        """发一趟请求，把连不上的各种姿势翻成人话。状态码的事交给调用方。"""
        try:
            return self._http.request(method, path, **kw)
        except httpx.ConnectTimeout as exc:
            # ConnectTimeout **不是** ConnectError 的子类（实测 httpx 0.28.1：
            # ConnectTimeout → TimeoutException → TransportError → HTTPError）。
            # 而 Windows 防火墙对没放行的入站是 **drop 而不是 reject**，所以
            # "对方没放行 8765"走的正是这一支。漏掉它，用户看到的是一串裸的
            # httpx 异常——这正是「启动了服务却连不上、且没有任何解释」。
            raise RemoteError(
                f"无法连接 hub（{self.config.hub_url}）：连接超时。"
                f"通常是因为防火墙未放行该端口（首次启动时请在提示中允许「专用网络」），"
                f"或该地址并非目标机器。（{exc}）"
            ) from exc
        except httpx.ConnectError as exc:
            text = str(exc).lower()
            if "getaddrinfo" in text or "name or service not known" in text \
                    or "nodename nor servname" in text:
                # 解析都失败了，跟 hub 起没起没关系——是地址本身的问题。
                raise RemoteError(
                    f"无法解析地址（{self.config.hub_url}）：域名查询失败。"
                    f"地址可能有误，或本机无法访问 DNS。（{exc}）"
                ) from exc
            raise RemoteError(
                f"无法连接 hub（{self.config.hub_url}）。请确认 hub 正在运行"
                f"（在服务器端界面点击「启动服务」，或运行 `python -m server.hub`）。"
                f"（{exc}）"
            ) from exc
        except httpx.TimeoutException as exc:
            # 读超时：命令跑太久或者 hub 卡住了。**不是**连接阶段的超时，
            # 所以不能说"防火墙"——那是两回事。
            raise RemoteError(f"等待 hub 响应超时（{self.config.hub_url}）：{exc}") from exc
        except httpx.HTTPError as exc:
            # 兜底：ReadError / WriteError / ProtocolError 之类
            raise RemoteError(f"与 hub 通信失败（{self.config.hub_url}）：{exc}") from exc

    def _call(self, method: str, path: str, **kw) -> dict[str, Any]:
        resp = self._request(method, path, **kw)
        if resp.status_code == 401 and self._refresh_token():
            resp = self._request(method, path, **kw)

        if resp.status_code >= 400:
            detail: Any
            try:
                detail = resp.json().get("detail", resp.text)
            except Exception:
                detail = resp.text
            raise RemoteError(f"[{resp.status_code}] {detail}", status=resp.status_code)
        return resp.json()

    # -- 状态 ---------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        return self._get("/api/status")

    def online(self) -> bool:
        try:
            return bool(self.status().get("agents"))
        except RemoteError:
            return False

    # -- 执行 ---------------------------------------------------------------
    def exec(self, cmd: str, *, agent: str | None = None, session: str = "main",
             cwd: str | None = None, shell: str | None = None,
             timeout_s: float | None = None, wait: bool = True) -> dict[str, Any]:
        return self._post("/api/exec", {
            "cmd": cmd, "agent": agent, "session": session, "cwd": cwd,
            "shell": shell, "timeout_s": timeout_s, "wait": wait,
        })

    def sessions(self, *, agent: str | None = None, session: str | None = None,
                 since_seq: int = 0) -> dict[str, Any]:
        params: dict[str, Any] = {"since_seq": since_seq}
        if agent:
            params["agent"] = agent
        if session:
            params["session"] = session
        return self._get("/api/sessions", **params)

    def stdin(self, data: str, *, agent: str | None = None, session: str = "main") -> dict:
        return self._post("/api/stdin", {"agent": agent, "session": session, "data": data})

    def signal(self, *, agent: str | None = None, session: str = "main",
               sig: str = "ctrl-c") -> dict:
        return self._post("/api/signal", {"agent": agent, "session": session, "sig": sig})

    def kill(self, *, agent: str | None = None, session: str = "main") -> dict:
        return self._post("/api/kill", {"agent": agent, "session": session})

    def ping(self, *, agent: str | None = None) -> dict:
        return self._post("/api/ping", {"agent": agent})

    # -- 消息 ---------------------------------------------------------------
    def send_chat(self, text: str, *, agent: str | None = None) -> dict[str, Any]:
        """发一句话给客户端（对方必须是图形界面版 agent）。"""
        return self._post("/api/chat", {"agent": agent, "text": text})

    # -- 文件 ---------------------------------------------------------------
    def put_file(self, local_path: str, remote_path: str, *,
                 agent: str | None = None) -> dict[str, Any]:
        return self._post("/api/files/put", {
            "local_path": local_path, "remote_path": remote_path, "agent": agent,
        })

    def get_file(self, remote_path: str, local_path: str, *,
                 agent: str | None = None,
                 max_bytes: int | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "remote_path": remote_path, "local_path": local_path, "agent": agent,
        }
        if max_bytes is not None:
            payload["max_bytes"] = max_bytes
        return self._post("/api/files/get", payload)

    # -- 屏幕 / 剪贴板 -------------------------------------------------------
    def screenshot(self, *, agent: str | None = None, monitor: int = 1,
                   save_to: str | None = None) -> dict[str, Any]:
        return self._post("/api/screen/shot",
                          {"agent": agent, "monitor": monitor, "save_to": save_to})

    def clipboard_get(self, *, agent: str | None = None) -> dict[str, Any]:
        return self._post("/api/clipboard/get", {"agent": agent})

    def clipboard_set(self, text: str, *, agent: str | None = None) -> dict[str, Any]:
        return self._post("/api/clipboard/set", {"agent": agent, "text": text})

"""把 hub 起在图形界面自己的进程里。

`python -m server.hub` 那条路一个字都没变——这里是**另一个**入口，只是省掉
「再开一个终端跑 hub」。所以这个模块要解决的都是「在同一个进程里再起一遍」才有的问题：

**一、`server/hub.py` 第 32 行是 import 时就执行的副作用**

```python
config: ServerConfig = load_config()
```

把 `config.toml` 和 4 个环境变量一次读死。所以「改用哪个配置文件」这件事，
**必须在 `import server.hub` 之前**就写进 `server.config` 的模块全局量里，
之后再 `hub.config = load_config(选中的路径)` 覆盖一次。顺序反了就是静默读错文件——
现象是「密钥明明改了却还是 401」，很难往回追。

**二、`server/audit.py` 第 16 行 `from .config import LOG_DIR` 是值绑定**

import 那一刻就把路径定死了。冻结成单文件 exe 时 `LOG_DIR` 会等于
`%TEMP%\\_MEIxxxxxx\\logs`——那个目录**退出即删**，等于服务端的审计日志一行没留。
所以 `LOG_DIR` 也要在 import 之前改。已经 import 过的话，还得回头改
`server.audit` 里那份，不然它抱着旧值不放。

**三、运行时换密钥要整个换掉 `hub.config` 对象**

`require_token`(48)、握手(108)、策略(336) 都是**调用时**读模块全局 `config`，
所以换密钥不能只改某个属性再指望别人看见：直接换对象。

`gui/app.py` 里对 `import server.hub` 是懒加载的（配置写坏了不能连客户端都起不来），
这个模块的 import 也全都放在函数里，`import gui.hubctl` 本身不碰 `server/`。
"""

from __future__ import annotations

import importlib
import logging
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import paths

log = logging.getLogger("gui.hubctl")

READY_TIMEOUT_S = 10.0
STOP_TIMEOUT_S = 10.0
FORCE_TIMEOUT_S = 3.0
_POLL_S = 0.05

READY = "on"
STARTING = "starting"
STOPPED = "off"
FAILED = "failed"


class HubControlError(RuntimeError):
    """起不来 / 停不掉的原因。消息可以直接显示在界面上。"""


class HubControl:
    """hub 的生命周期。非线程安全——只在 UI 主线程里改状态。

    `start()` 只是把线程点着就返回，**不阻塞**；要知道起没起得来用
    `wait_ready()`（工作线程里调）或者轮询 `state`（UI 线程里的定时器）。
    起 hub 通常在一秒内，但端口被占用时会走到 `FAILED`，界面得能说出来。
    """

    def __init__(
        self,
        *,
        config_module: Any | None = None,
        hub_loader: Callable[[], Any] | None = None,
        uvicorn_loader: Callable[[], Any] | None = None,
    ) -> None:
        self._config_module = config_module
        self._hub_loader = hub_loader or (lambda: importlib.import_module("server.hub"))
        # 同上：都是给测试注入假模块用的，默认走真实实现。
        self._uvicorn_loader = uvicorn_loader or (lambda: importlib.import_module("uvicorn"))

        self._hub: Any | None = None
        self._server: Any | None = None
        self._thread: threading.Thread | None = None
        self._error: BaseException | None = None
        self.config: Any | None = None
        self.choice: Any | None = None
        self.log_dir: Path | None = None

    # -- 准备 ---------------------------------------------------------------
    def prepare(self, choice: Any, *, log_dir: str | Path | None = None) -> Any:
        """按 `choice`（`gui.paths.ConfigChoice`）把 hub 模块备好，返回 config。

        幂等：重复调用只是再读一遍文件，方便界面上「重新读取配置」。
        """
        path = Path(choice.path)
        cfgmod = self._config_module or importlib.import_module("server.config")
        target_log_dir = paths.log_dir() if log_dir is None else Path(log_dir)

        # ↓↓↓ 这两行必须在 import server.hub 之前。别调换顺序。
        cfgmod.DEFAULT_CONFIG_PATH = path
        cfgmod.LOG_DIR = target_log_dir

        # server.audit 可能已经被别的东西 import 过了，那句 `from .config import LOG_DIR`
        # 早就把旧值绑死在它自己的命名空间里。光改 server.config.LOG_DIR 追不回来。
        already = sys.modules.get("server.audit")
        if already is not None:
            already.LOG_DIR = target_log_dir

        hub = self._hub_loader()
        # 走到这里 server.hub 才第一次执行 `config = load_config()`（读的是默认路径）。
        # 下面这次是真正按我们选中的文件读一遍，把上面那次的副作用纠正回来。
        self.config = cfgmod.load_config(path)
        self._hub = hub
        hub.config = self.config

        self.choice = choice
        self.log_dir = target_log_dir
        return self.config

    def apply(self, cfg: Any) -> None:
        """运行时换配置（改密钥、换模式）。**整个对象换掉**，见模块文档第三条。"""
        if self._hub is None:
            raise HubControlError("hub 还没 prepare，先把配置文件选好")
        self._hub.config = cfg
        self.config = cfg

    # -- 起 -----------------------------------------------------------------
    def start(self, cfg: Any | None = None) -> None:
        """点着 hub 线程就返回。已经起过就什么也不做。"""
        if self.running:
            return
        if self._hub is None:
            raise HubControlError("hub 还没 prepare，先把配置文件选好")
        if cfg is not None:
            self.apply(cfg)

        uvicorn = self._uvicorn_loader()
        # log_config=None 是刻意的：uvicorn 0.41 在 log_config=None 时会跳过 dictConfig，
        # 但仍然应用 log_level，并且**不装自己的 handler**，于是日志一路冒泡到 root——
        # 控制台黑框和图形界面就都看得见了。
        uconf = uvicorn.Config(
            self._hub.app,
            host=self.config.host,
            port=self.config.port,
            log_level="warning",
            log_config=None,
        )
        self._server = uvicorn.Server(uconf)
        self._error = None
        self._thread = threading.Thread(target=self._run, name="hub", daemon=True)
        self._thread.start()
        log.info("hub 启动于 %s:%d（模式 %s）", self.config.host, self.config.port,
                 self.config.mode)

    def _run(self) -> None:
        """hub 线程。只跑 uvicorn，绝不碰 UI。"""
        try:
            self._server.run()                       # type: ignore[union-attr]
        except BaseException as exc:                 # 端口被占时 uvicorn 会 sys.exit(1)
            self._error = exc
            log.error("hub 退出：%s", exc)

    # -- 看 -----------------------------------------------------------------
    @property
    def module(self) -> Any | None:
        """`prepare()` 之后就是 `server.hub` 模块本身。

        服务器端页面用它注册消息监听器、拿 hub 的进程内状态——都在同一个进程里，
        没必要再绕一层 HTTP。还没 prepare 时是 None。
        """
        return self._hub

    @property
    def server(self) -> Any | None:
        return self._server

    @property
    def thread(self) -> threading.Thread | None:
        return self._thread

    @property
    def error(self) -> BaseException | None:
        return self._error

    @property
    def running(self) -> bool:
        """hub 是不是还在跑。**判据里必须有 `_error`。**

        少了它，起不来的 hub 会有一小段时间自称"还在跑"：`_run` 里
        `self._error = exc` 和线程真正退出之间隔着那行 `log.error`（要写文件、
        要写控制台），于是 `state == FAILED` 而 `running` 还是 True。
        界面据此会去 `stop()` 一个已经死掉的 hub，测试也会随缘变红——
        而"FAILED 却不 running"本来就是唯一说得通的说法。
        """
        if self._error is not None:
            return False
        thread = self._thread
        if thread is None or not thread.is_alive():
            return False
        server = self._server
        return server is not None and not server.should_exit

    @property
    def ready(self) -> bool:
        server = self._server
        return bool(server is not None and server.started and self._error is None)

    @property
    def state(self) -> str:
        if self._error is not None:
            return FAILED
        if self.ready:
            return READY
        if self._thread is not None and self._thread.is_alive():
            return STARTING
        return STOPPED

    def wait_ready(self, timeout: float = READY_TIMEOUT_S) -> bool:
        """等到 uvicorn 真的 `started` 为止。**阻塞**——在工作线程里调。"""
        deadline = time.monotonic() + timeout
        while True:
            if self._error is not None:
                return False
            if self.ready:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(_POLL_S)

    # -- 停 -----------------------------------------------------------------
    def stop(self, timeout: float = STOP_TIMEOUT_S) -> bool:
        """停 hub。返回 True = 已经停干净了。

        返回 False 不是错误：正在跑的文件传输最长 300 秒，uvicorn 会等它收尾。
        **界面在这时必须说出来**（「有请求还在跑」），而不是静默卡住或者假装停了。
        想强行收摊用 `force_stop()`——那会掐断正在传的文件。
        """
        server = self._server
        thread = self._thread
        if server is None:
            return True
        server.should_exit = True
        if thread is not None:
            thread.join(timeout)
        if thread is not None and thread.is_alive():
            return False
        self._server = None
        self._thread = None
        return True

    def force_stop(self, timeout: float = FORCE_TIMEOUT_S) -> bool:
        """不等在途请求，直接收摊。传了一半的文件会断——界面上要问过再用。"""
        server = self._server
        if server is not None:
            server.force_exit = True
        return self.stop(timeout)

    def reset(self) -> None:
        """丢掉上次的失败状态，好让界面重试一次。"""
        self._error = None
        if self._thread is not None and not self._thread.is_alive():
            self._thread = None
            self._server = None

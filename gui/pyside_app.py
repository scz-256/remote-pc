"""PySide6 图形界面。

这一层只负责展示和用户交互，连接协议仍然复用项目现有的
``agent.Agent``、``gui.hubctl.HubControl`` 和 ``gui.conninfo``。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
from pathlib import Path

from . import chat, linkcheck, netinfo, paths, store, toml_edit
from .conninfo import ConnInfo, ConnInfoError, extract, parse

log = logging.getLogger("gui.pyside")
_DLL_HANDLES = []


def _qt():
    """延迟导入 Qt，保证 ``import gui.app`` 依旧是轻量的。"""
    if getattr(sys, "frozen", False):
        bundle = Path(getattr(sys, "_MEIPASS", ""))
        # PyInstaller 6.20+ places collected files below ``_internal``.
        # Older bootloaders put them directly below ``_MEIPASS``; support
        # both layouts so the same exe remains portable across builders.
        internal = bundle / "_internal"
        runtime_root = internal if internal.is_dir() else bundle
        for folder in (runtime_root, runtime_root / "PySide6", runtime_root / "shiboken6"):
            if folder.is_dir() and hasattr(os, "add_dll_directory"):
                try:
                    _DLL_HANDLES.append(os.add_dll_directory(str(folder)))
                except OSError:
                    pass
        # 某些 Qt6 依赖通过传统 PATH 查找；onefile 解包后显式加入两处 DLL 目录。
        dll_paths = [str(runtime_root), str(runtime_root / "PySide6"), str(runtime_root / "shiboken6")]
        os.environ["PATH"] = os.pathsep.join(dll_paths + [os.environ.get("PATH", "")])
    from PySide6 import QtCore, QtGui, QtWidgets

    return QtCore, QtGui, QtWidgets


def _stylesheet() -> str:
    return """
    QWidget { font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif; color: #182230; }
    QMainWindow, QWidget#page, QScrollArea, QScrollArea > QWidget > QWidget { background: #f5f7fb; }
    QFrame#sidebar { background: #111827; }
    QLabel#brand { color: #f8fafc; font-size: 20px; font-weight: 700; }
    QLabel#brandSub { color: #94a3b8; font-size: 11px; }
    QLabel#pageTitle { color: #111827; font-size: 26px; font-weight: 700; }
    QLabel#pageSub { color: #64748b; font-size: 13px; }
    QPushButton { border: 1px solid #d8e0eb; border-radius: 8px; padding: 8px 14px; background: #ffffff; }
    QPushButton:hover { border-color: #4f8cff; background: #f4f8ff; }
    QPushButton:pressed { background: #e9f1ff; }
    QPushButton#primary { color: white; border: 0; background: #3678f6; font-weight: 600; }
    QPushButton#primary:hover { background: #2869e8; }
    QPushButton#danger { color: white; border: 0; background: #dc5b63; font-weight: 600; }
    QPushButton#danger:hover { background: #c94b54; }
    QPushButton#nav { text-align: left; color: #cbd5e1; border: 0; background: transparent; padding: 11px 14px; }
    QPushButton#nav:hover { color: white; background: #1e293b; }
    QPushButton#nav[active="true"] { color: white; background: #294d89; }
    QFrame#card, QFrame#roleCard { background: white; border: 1px solid #e3e9f2; border-radius: 14px; }
    QFrame#roleCard:hover { border: 1px solid #6e9efb; }
    QLabel#cardTitle { color: #172033; font-size: 15px; font-weight: 700; }
    QLabel#muted { color: #64748b; }
    QLabel#metric { color: #172033; font-size: 24px; font-weight: 700; }
    QLabel#badge { border-radius: 9px; padding: 4px 10px; font-weight: 600; }
    QLineEdit, QComboBox, QPlainTextEdit { background: #ffffff; border: 1px solid #d8e0eb; border-radius: 8px; padding: 8px; }
    QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus { border: 1px solid #4f8cff; }
    QPlainTextEdit { font-family: Consolas, "Cascadia Mono", monospace; }
    QProgressBar { border: 0; border-radius: 4px; background: #e8edf5; height: 8px; }
    QProgressBar::chunk { border-radius: 4px; background: #3678f6; }
    QScrollBar:vertical { background: transparent; width: 10px; }
    QScrollBar::handle:vertical { background: #cbd5e1; border-radius: 5px; min-height: 30px; }
    """


def _label(text: str, object_name: str = ""):
    _, _, QtWidgets = _qt()
    w = QtWidgets.QLabel(text)
    if object_name:
        w.setObjectName(object_name)
    return w


def _card(title: str, subtitle: str = ""):
    _, _, QtWidgets = _qt()
    frame = QtWidgets.QFrame()
    frame.setObjectName("card")
    layout = QtWidgets.QVBoxLayout(frame)
    layout.setContentsMargins(18, 16, 18, 16)
    layout.setSpacing(8)
    layout.addWidget(_label(title, "cardTitle"))
    if subtitle:
        layout.addWidget(_label(subtitle, "muted"))
    return frame, layout


class AgentRunner:
    """在独立线程运行 agent，事件通过 Qt 信号回到界面线程。"""

    def __init__(self, cfg: dict, QtCore):
        # 告诉 hub 这条连接来自图形界面，服务器端据此判断"消息"能不能用。
        # 调用方的 dict 不动——同一份配置还可能被别处复用。
        self._cfg = {**cfg, "ui": "gui"}
        self._qt = QtCore
        self._thread = threading.Thread(target=self._run, daemon=True, name="pyside-agent")
        self._agent = None
        self._stop_requested = threading.Event()
        self.on_event = None

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        try:
            # Agent 内部有绑定事件循环的 asyncio.Lock，必须在这个线程里构造。
            # 构造也要放进 try，否则审计路径/配置出错时线程会静默崩掉，界面永远停在“连接中”。
            from agent.agent import Agent

            agent = Agent(self._cfg)
            self._agent = agent
            agent.on_event = self._emit
            if self._stop_requested.is_set():
                agent.stop()
            asyncio.run(agent.run())
        except Exception as exc:  # agent 自己会记录可解释的连接错误
            self._emit("fatal", {"text": str(exc)})
        finally:
            # 线程结束之后就不该再有"发出去"的消息：agent 的事件循环已经关了。
            self._agent = None
            self._emit("finished", {})

    def _emit(self, kind: str, fields: dict) -> None:
        callback = self.on_event
        if callback is not None:
            callback(kind, fields)

    def stop(self) -> None:
        self._stop_requested.set()
        agent = self._agent
        if agent is not None:
            agent.stop()

    def send_chat(self, text: str) -> bool:
        """发一条消息给服务器端。未连接时返回 False。"""
        if self._stop_requested.is_set():
            return False
        agent = self._agent
        return bool(agent is not None and agent.send_chat(text))


def _run_on_thread(fn, done):
    """在线程里执行 ``fn``，以 ``(result, error)`` 回调报告结果。"""
    def run():
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001 - 交给界面显示
            done(None, exc)
        else:
            done(result, None)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def _text(widget) -> str:
    return widget.text().strip()


def _post_chat(port: int, token: str, text: str) -> str:
    """把一条消息交给本机 hub。在**工作线程**里跑——这是 HTTP 调用，不能卡界面。

    这里自己拼一个 ServerConfig，而不是复用 `load_config()` 的结果：
    后者的 `hub_url` 是按配置文件里的端口算出来的，而界面上可能刚改过端口，
    拿错了就会发到没人监听的地址。密钥只用来发这一次请求，绝不写进日志。
    """
    from server.config import ServerConfig
    from server.remote import RemoteClient

    cfg = ServerConfig(token=token, port=port, hub_url=f"http://127.0.0.1:{port}")
    with RemoteClient(cfg, timeout_s=10.0) as client:
        client.send_chat(text)
    return text


def _window_is_active(widget) -> bool:
    """窗口是不是在前台。判断不了的时候当作在前台——宁可少闪一次。"""
    try:
        window = widget.window()
        return bool(window is None or window.isActiveWindow())
    except Exception:  # noqa: BLE001 - 界面正在销毁
        return True


def _alert_window(widget) -> None:
    """闪任务栏。不弹窗也不出声：消息可能只是"稍后再看"的一句话。"""
    try:
        _qt()[2].QApplication.alert(widget)
    except Exception:  # noqa: BLE001 - 有些平台/无窗口平台上不支持
        pass


class ChatPanel:
    """消息卡片：上面是只读记录，下面是单行输入。

    只负责展示和收集输入；怎么发出去由 `on_send` 决定——服务器端走本机 HTTP，
    客户端走内嵌 agent 的那条连接。
    """

    MIN_HEIGHT = 110

    def __init__(self, on_send):
        _, _, QtWidgets = _qt()
        self.on_send = on_send
        self._enabled: bool | None = None
        self.frame, layout = _card("消息", "仅本次会话可见，不写入日志或审计记录。")
        self.view = QtWidgets.QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setMinimumHeight(self.MIN_HEIGHT)
        # 长会话不把内存吃光；这份记录本来也不打算留档。
        self.view.setMaximumBlockCount(5000)
        layout.addWidget(self.view)
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(8)
        self.input = QtWidgets.QLineEdit()
        self.input.returnPressed.connect(self._submit)
        self.send_btn = QtWidgets.QPushButton("发送")
        self.send_btn.setObjectName("primary")
        self.send_btn.clicked.connect(self._submit)
        row.addWidget(self.input, 1)
        row.addWidget(self.send_btn)
        layout.addLayout(row)
        self.set_enabled(False)

    def _submit(self):
        if not self._enabled:
            return
        raw = self.input.text()
        text = chat.normalize(raw)
        if text is None:
            if raw.strip():
                self.system(f"消息未发送：单条上限 {chat.MAX_CHAT_CHARS} 字符。")
            return
        if self.on_send is not None:
            self.on_send(text)

    def append(self, who: str, text: str, ts=None) -> None:
        self.view.appendPlainText(chat.format_line(who, text, ts))
        bar = self.view.verticalScrollBar()
        bar.setValue(bar.maximum())

    def system(self, text: str) -> None:
        """系统提示（发送失败、未连接等），和双方的消息排在同一个记录里。"""
        self.view.appendPlainText(text)
        bar = self.view.verticalScrollBar()
        bar.setValue(bar.maximum())

    def clear_input(self) -> None:
        self.input.clear()

    def set_enabled(self, enabled: bool, hint: str = "") -> None:
        enabled = bool(enabled)
        hint = hint or ("输入消息，按回车发送" if enabled else "连接建立后可发送消息")
        if enabled == self._enabled and hint == self.input.placeholderText():
            return
        self._enabled = enabled
        self.input.setEnabled(enabled)
        self.send_btn.setEnabled(enabled)
        self.input.setPlaceholderText(hint)


class RoleCard:
    def __init__(self, icon: str, title: str, desc: str, on_click):
        QtCore, _, QtWidgets = _qt()
        self.frame = QtWidgets.QFrame()
        self.frame.setObjectName("roleCard")
        self.frame.setCursor(QtCore.Qt.PointingHandCursor)
        layout = QtWidgets.QVBoxLayout(self.frame)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(8)
        top = QtWidgets.QHBoxLayout()
        top.addWidget(_label(icon))
        top.addStretch()
        top.addWidget(_label("›", "metric"))
        layout.addLayout(top)
        layout.addWidget(_label(title, "cardTitle"))
        layout.addWidget(_label(desc, "muted"))
        button = QtWidgets.QPushButton("进入工作台")
        button.setObjectName("primary")
        button.clicked.connect(on_click)
        layout.addWidget(button)


class WelcomePage:
    def __init__(self, host):
        _, _, QtWidgets = _qt()
        self.widget = QtWidgets.QWidget(objectName="page")
        root = QtWidgets.QVBoxLayout(self.widget)
        root.setContentsMargins(34, 30, 34, 30)
        root.setSpacing(18)
        root.addWidget(_label("开始使用", "pageTitle"))
        root.addWidget(_label("请选择这台电脑在本次会话中承担的角色。所有状态将集中显示在工作台中。", "pageSub"))
        cards = QtWidgets.QHBoxLayout()
        cards.setSpacing(16)
        server = RoleCard("🖥", "服务器端", "在本机启动 hub，查看在线设备并发起远程操作。", lambda: host.show_page("server"))
        client = RoleCard("🔗", "客户端", "连接对端 hub，实时查看连接状态与 agent 事件。", lambda: host.show_page("client"))
        cards.addWidget(server.frame)
        cards.addWidget(client.frame)
        root.addLayout(cards)

        recent_frame, recent = _card("最近连接", "列出成功连接过的地址，便于快速恢复连接。")
        remembered = store.load()
        if remembered is None:
            recent.addWidget(_label("暂无记录", "muted"))
        else:
            row = QtWidgets.QHBoxLayout()
            row.addWidget(_label(f"{remembered.conn.host}:{remembered.conn.port}", "cardTitle"))
            row.addStretch()
            b = QtWidgets.QPushButton("打开客户端")
            b.clicked.connect(lambda: host.show_page("client", remembered.conn))
            row.addWidget(b)
            recent.addLayout(row)
        root.addWidget(recent_frame)
        root.addStretch()


class ServerPage:
    def __init__(self, host, config_path=None):
        QtCore, _, QtWidgets = _qt()
        self.host = host
        self.config_path = config_path
        class _StopBridge(QtCore.QObject):
            done = QtCore.Signal(object, object)
        self._stop_bridge = _StopBridge()
        self._stop_bridge.done.connect(self._after_stop)
        # 启动后的自检同样在别的线程里跑，回调**只允许 emit**，不能碰控件。
        class _VerifyBridge(QtCore.QObject):
            done = QtCore.Signal(object, object)
        self._verify_bridge = _VerifyBridge()
        self._verify_bridge.done.connect(self._after_verify)
        self._verify_busy = False
        # hub 收到客户端消息时会在**它的事件循环线程**里调 _chat_sink，
        # 所以那个回调只允许 emit；真正的界面更新在下面的槽里做。
        class _ChatBridge(QtCore.QObject):
            received = QtCore.Signal(str, str, object)
            sent = QtCore.Signal(str, object, object)
        self._chat_bridge = _ChatBridge()
        self._chat_bridge.received.connect(self._on_chat_received)
        self._chat_bridge.sent.connect(self._after_chat_sent)
        self._chat_listener = self._chat_sink
        self._chat_busy = False
        self._verified = False
        self._closed = False
        # 界面上这把密钥和磁盘上那把是不是同一把。用来判断"启动前要不要写回"。
        self._token_on_disk = ""
        self.widget = QtWidgets.QWidget(objectName="page")
        root = QtWidgets.QVBoxLayout(self.widget)
        root.setContentsMargins(34, 30, 34, 24)
        root.setSpacing(16)
        heading = QtWidgets.QHBoxLayout()
        title_box = QtWidgets.QVBoxLayout()
        title_box.addWidget(_label("服务器端工作台", "pageTitle"))
        title_box.addWidget(_label("启动 hub，并将连接串安全地提供给对端。", "pageSub"))
        heading.addLayout(title_box)
        heading.addStretch()
        self.status = _label("未启动", "badge")
        self._set_status("off")
        heading.addWidget(self.status, 0, QtCore.Qt.AlignTop)
        root.addLayout(heading)

        cards = QtWidgets.QHBoxLayout()
        cards.setSpacing(12)
        self.ip_metric = self._metric("本机地址", "探测中…")
        self.port_metric = self._metric("监听端口", "8765")
        self.agent_metric = self._metric("在线设备", "—")
        for c in (self.ip_metric[0], self.port_metric[0], self.agent_metric[0]): cards.addWidget(c)
        root.addLayout(cards)

        share, share_layout = _card("连接信息", "复制完整连接串发送给对端，客户端粘贴后即可解析。")
        self.ip_combo = QtWidgets.QComboBox()
        self.port = QtWidgets.QLineEdit("8765")
        self.token = QtWidgets.QLineEdit()
        self.token.setEchoMode(QtWidgets.QLineEdit.Password)
        self.link = QtWidgets.QLineEdit()
        self.link.setReadOnly(True)
        # 以前只有"重新探测"会刷新连接串，于是改了密钥或端口之后，框里还是旧的那条，
        # 复制出去的就是错的。改了哪一项都跟着重算。
        self.ip_combo.currentIndexChanged.connect(self._on_link_input_changed)
        self.port.textChanged.connect(self._refresh_link)
        self.token.textChanged.connect(self._refresh_link)
        # 手改密钥也要落地——README 承诺过"界面里改的就是 MCP/CLI 读的那一份"。
        # editingFinished 是"改完了"（失焦或回车），不是每敲一个字符都写一次。
        self.token.editingFinished.connect(self._token_edited)
        self._refresh_ips()
        form = QtWidgets.QFormLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)
        form.setRowWrapPolicy(QtWidgets.QFormLayout.WrapLongRows)
        form.setFieldGrowthPolicy(QtWidgets.QFormLayout.AllNonFixedFieldsGrow)
        form.addRow("本机 IPv4", self.ip_combo)
        form.addRow("端口", self.port)
        form.addRow("预共享密钥", self.token)
        form.addRow("连接串", self.link)
        share_layout.addLayout(form)
        actions = QtWidgets.QHBoxLayout()
        self.refresh_btn = QtWidgets.QPushButton("重新探测")
        self.refresh_btn.clicked.connect(self._refresh_ips)
        self.gen_btn = QtWidgets.QPushButton("随机生成密钥")
        self.gen_btn.clicked.connect(self._generate_token)
        self.copy_btn = QtWidgets.QPushButton("复制连接串")
        self.copy_btn.setObjectName("primary")
        self.copy_btn.clicked.connect(self._copy_link)
        self.start_btn = QtWidgets.QPushButton("启动服务")
        self.start_btn.setObjectName("primary")
        self.start_btn.clicked.connect(self._toggle_hub)
        actions.addWidget(self.refresh_btn)
        actions.addWidget(self.gen_btn)
        actions.addStretch()
        actions.addWidget(self.copy_btn)
        actions.addWidget(self.start_btn)
        share_layout.addLayout(actions)

        # 两行提醒，都是"点下去之前就该知道"的事：
        #   ① 密钥到底写进了哪个文件（README 承诺过：改的就是 MCP/CLI 读的那一份）
        #   ② 这个本机地址对方够不够得着
        self.config_hint = _label("", "muted")
        self.config_hint.setWordWrap(True)
        share_layout.addWidget(self.config_hint)
        self.ip_hint = _label("", "muted")
        self.ip_hint.setWordWrap(True)
        share_layout.addWidget(self.ip_hint)
        root.addWidget(share)

        log_card, log_layout = _card("活动日志", "记录 hub 生命周期与界面操作结果。")
        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(140)
        log_layout.addWidget(self.log)

        # 消息和日志并排放在底部：竖着摞的话，默认窗口高度下输入框会被挤到
        # 折叠线以下，等于没有。
        self.chat = ChatPanel(self._send_chat)
        self.chat.set_enabled(False, "启动服务后可发送消息")
        bottom = QtWidgets.QHBoxLayout()
        bottom.setSpacing(12)
        bottom.addWidget(self.chat.frame, 5)
        bottom.addWidget(log_card, 7)
        root.addLayout(bottom, 1)

        self.hub = None
        self.choice = None
        self.cfg = None
        self.timer = QtCore.QTimer(self.widget)
        self.timer.timeout.connect(self._poll)
        self.timer.start(800)
        self._refresh_ips()
        self._load_config()

    def _metric(self, title, value):
        _, _, QtWidgets = _qt()
        card, layout = _card(title)
        label = _label(value, "metric")
        layout.addWidget(label)
        return card, label

    def _write(self, text: str):
        self.log.appendPlainText(text)

    def _confirm(self, text: str, *, title: str = "确认") -> bool:
        """请求确认后再执行。默认按钮为「取消」，回车不会误触。

        只在"做了就不好撤销"的地方用（换密钥会波及 hub / MCP / CLI 三边）。
        """
        QtWidgets = _qt()[2]
        box = QtWidgets.QMessageBox(self.widget)
        box.setIcon(QtWidgets.QMessageBox.Warning)
        box.setWindowTitle(title)
        box.setText(text)
        go = box.addButton("继续", QtWidgets.QMessageBox.AcceptRole)
        quit_btn = box.addButton("取消", QtWidgets.QMessageBox.RejectRole)
        box.setDefaultButton(quit_btn)
        box.setEscapeButton(quit_btn)
        box.exec()
        return box.clickedButton() is go

    def _set_status(self, state: str):
        colors = {"on": ("在线", "#dcfce7", "#15803d"), "starting": ("启动中", "#fef3c7", "#b45309"), "failed": ("启动失败", "#fee2e2", "#b91c1c"), "off": ("未启动", "#e2e8f0", "#475569")}
        text, bg, fg = colors.get(state, colors["off"])
        self.status.setText(text)
        self.status.setStyleSheet(f"background:{bg};color:{fg};")

    def _refresh_ips(self):
        ips = netinfo.list_lan_ipv4()
        current = self.ip_combo.currentText() if hasattr(self, "ip_combo") else ""
        self.ip_combo.clear()
        self.ip_combo.addItems(ips or ["127.0.0.1"])
        if current and current in ips: self.ip_combo.setCurrentText(current)
        if hasattr(self, "ip_metric"): self.ip_metric[1].setText(self.ip_combo.currentText())
        self._refresh_link()
        self._refresh_ip_hint()

    def _on_link_input_changed(self):
        self._refresh_link()
        self._refresh_ip_hint()

    def _refresh_link(self):
        if not hasattr(self, "link"): return
        host = self.ip_combo.currentText() if self.ip_combo.count() else "127.0.0.1"
        try: port = int(self.port.text() or 8765)
        except ValueError: port = 8765
        token = self.token.text().strip()
        self.link.setText(f"ws://{host}:{port}#{token}" if token else "请先填写密钥")
        if hasattr(self, "port_metric"): self.port_metric[1].setText(str(port))

    def _refresh_ip_hint(self):
        """服务器端自己先看一眼：这张网卡上的地址，对方够不够得着。

        `netinfo.address_allowed` 的 docstring 写着「界面必须替他问出这句话」，
        而它一直没人调用——于是地址落在 allowed_networks 之外时，对方只看到
        「连不上」，两边都要猜半天。
        """
        if not hasattr(self, "ip_hint"): return
        ip = self.ip_combo.currentText() if self.ip_combo.count() else ""
        cfg = getattr(self, "cfg", None)
        notes: list[str] = []
        if not ip:
            notes.append("未探测到局域网地址。当前显示的可能是 127.0.0.1，对端将无法连接。")
        elif not netinfo.is_usable(ip):
            notes.append(f"⚠ {ip} 仅本机可用（127.x / 169.254.x），对端无法连接。请更换网卡。")
        elif cfg is None:
            pass                       # 配置还没读出来，先不下结论
        elif not cfg.allowed_networks:
            notes.append("⚠ allowed_networks 为空：hub 将拒绝所有连接。"
                         "请在 config.toml 中填写允许的网段。")
        elif not netinfo.address_allowed(ip, cfg.allowed_networks):
            notes.append(
                f"⚠ {ip} 不在 allowed_networks（{'、'.join(cfg.allowed_networks)}）中，"
                f"对端连接将被拒绝。请将该地址对应的网段加入 allowed_networks，或更换网卡。")
        self.ip_hint.setText(" ".join(notes))

    def _load_config(self):
        from server.config import generate_token, load_config
        self.choice = paths.resolve_config_path(explicit=self.config_path)
        # 「用了哪个文件、怎么找到的」要说出来，否则"密钥到底写哪去了"只能靠猜。
        self._write(f"配置文件：{self.choice.path}（{self.choice.source}）")
        try:
            self.cfg = load_config(self.choice.path)
        except Exception as exc:
            self.cfg = None
            self._write(f"读取配置失败，已改用默认值：{exc}")
        if self.cfg is not None:
            self.port.setText(str(self.cfg.port))
            self.token.setText(self.cfg.token)
        self._token_on_disk = self.token.text().strip()

        if self._token_on_disk:
            self._announce_config()
        else:
            # 以前这里是 `self.cfg.token or generate_token()`：界面上凭空多出一把
            # 文件里没有的密钥。照着它把连接串发给对方，hub 读到空 token 直接回
            # 500——正是"启动了服务但客户端连不上"的一个来源。既然要用，就落地。
            self.token.setText(generate_token())
            self._write("配置文件中尚无密钥，已生成并写回。")
            self._save_token()
        self._refresh_link()
        self._refresh_ip_hint()

    def _announce_config(self):
        if self.choice is None: return
        exists = self.choice.path.is_file()
        self.config_hint.setText(
            f"密钥所在文件：{self.choice.path}（{self.choice.source}）"
            + ("" if exists else "（尚未写入）"))

    def _save_token(self) -> bool:
        """把界面上这把密钥写回配置文件。写不进就**明说**，绝不悄悄写到别处去。"""
        if self.choice is None:
            return False
        token = self.token.text().strip()
        result = toml_edit.save_token_to_config(self.choice.path, token)
        if result.ok:
            self._token_on_disk = token
            self._write(result.text)
            self._push_to_hub()
        else:
            self._write(f"⚠ 密钥写入配置文件失败：{result.text}")
            self._write("  该密钥仅在本次会话内有效；重启界面后将恢复为文件中的密钥。")
        self._announce_config()
        return result.ok

    def _hub_running(self) -> bool:
        return self.hub is not None and bool(getattr(self.hub, "running", False))

    def _push_to_hub(self):
        """把刚写进文件的配置推给**正在跑**的 hub。

        不推会怎样：`server/hub.py` 的鉴权（HTTP 的 `require_token`、agent 握手的
        HMAC 校验）读的都是模块级那个 `config`，而 hub 从被 import 起就拿着它。
        界面改了密钥、也写进了 config.toml，但 hub 内存里还是旧的 —— 于是
        **界面显示新密钥、文件里也是新密钥、而 hub 认旧密钥**，新客户端拿着刚
        复制走的连接串过来，当场 4401。`HubControl.apply()` 换的就是那个
        `config`，推过去之后新连接立刻生效，不用重启服务。

        已经在线的 agent 不受影响：鉴权只在握手时做一次。它们要等到断线重连
        才会因为揣着旧密钥被拒。
        """
        if not self._hub_running() or self.choice is None:
            return
        try:
            from server.config import load_config
            cfg = load_config(self.choice.path)
        except Exception as exc:      # noqa: BLE001 - 读不回来就明说，别装作推过了
            self._write(f"⚠ 无法重新读取配置（{exc}）。服务重启后才会生效。")
            return
        try:
            cfg.port = int(self.port.text())
        except ValueError:
            pass
        try:
            self.hub.apply(cfg)
        except Exception as exc:      # noqa: BLE001 - HubControlError 之类
            self._write(f"⚠ 新密钥未能同步至 hub（{exc}）。服务重启后才会生效。")
            return
        self.cfg = cfg
        self._write("新密钥已同步至运行中的 hub，新连接立即生效，无需重启。")
        self._write("  （命令行客户端与 CC 仍持有旧密钥，将在下次调用时自动重新读取。）")

    def _token_edited(self):
        """输入框里的密钥改完了（失焦/回车）。空的不写——那是还没打完。"""
        token = self.token.text().strip()
        if not token or token == self._token_on_disk:
            return
        self._write("密钥已修改，正在写回配置文件。")
        self._save_token()

    def _generate_token(self):
        from server.config import generate_token
        # 换密钥是个**全局**动作：hub、config.toml、MCP、CLI 都得跟着换。
        # hub 正跑着的时候换，代价是具体而有限的，所以先把话说清楚再动手——
        # 不是禁用（密钥真泄露了你正需要换），是让人知道自己要付什么。
        if self._hub_running() and not self._confirm(
            "hub 正在运行。更换密钥将产生以下影响：\n\n"
            "· 已连接的设备不会立即掉线（鉴权仅在握手时执行一次）\n"
            "· 设备断线重连时将被拒绝，需要重新分发连接串\n"
            "· 命令行客户端与 CC 将在下次调用时自动重新读取\n\n"
            "确认更换密钥？",
            title="更换密钥",
        ):
            self._write("已取消，密钥未发生变化。")
            return
        self.token.setText(generate_token())
        self._write("已生成新的随机密钥。")
        # 生成即写回：不然界面换了、MCP/CLI/下次启动读到的还是旧的。
        self._save_token()
        self._refresh_link()

    def _copy_link(self):
        self._refresh_link()
        QtWidgets = _qt()[2]
        QtWidgets.QApplication.clipboard().setText(self.link.text())
        self._write("连接串已复制到剪贴板。")

    def _toggle_hub(self):
        if self.hub is not None and self.hub.running:
            self.start_btn.setEnabled(False)
            self._write("正在停止 hub…")
            hub = self.hub
            _run_on_thread(hub.stop,
                           lambda result, error: self._stop_bridge.done.emit(result, error))
            return
        from .hubctl import HubControl
        from server.config import load_config
        self._refresh_link()
        token = self.token.text().strip()
        if token and token != self._token_on_disk:
            # 界面上这把和文件里那把不是同一把。hub 读的是文件，MCP/CLI 读的也是
            # 文件——不写回去的话，只有这个界面认得新密钥。
            self._write("界面密钥与配置文件不一致，正在写回配置文件。")
            if not self._save_token():
                self._write("  （写入失败：MCP 与 CLI 仍将使用文件中的旧密钥）")
        try:
            self.cfg = load_config(self.choice.path if self.choice else None)
            self.cfg.port = int(self.port.text())
            self.cfg.token = token
            self.hub = self.hub or HubControl()
            # prepare 会重新按选中路径读一遍文件——所以上面那次写回必须在它前面。
            self.hub.prepare(self.choice)
            # 监听器必须赶在 start() 之前挂上：agent 握手时 hub 会回答
            # "服务器端有没有消息面板"，晚一步客户端那边就会一直显示不支持。
            self._attach_chat()
            self.hub.start(self.cfg)
            self._verified = False
            self._write(f"hub 正在监听 {self.cfg.host}:{self.cfg.port}…")
            self.start_btn.setEnabled(False)
        except Exception as exc:
            self._set_status("failed")
            self._write(f"启动失败：{exc}")

    def _after_stop(self, stopped, error):
        self.start_btn.setEnabled(True)
        if error is not None:
            self._write(f"停止失败：{error}")
            self._set_status("failed")
        elif stopped:
            self._write("hub 已停止。")
            self._set_status("off")
            self._verified = False
        else:
            # 有在途请求时 HubControl.stop() 会超时返回 False，不能把仍在运行的
            # 服务显示成已停止，否则下一次点击会再起一个 hub。
            self._write("hub 仍有请求未完成，尚未完全停止。")
            self._set_status("on")

    def _poll(self):
        state = self.hub.state if self.hub is not None else "off"
        self._set_status(state)
        self.start_btn.setEnabled(state != "starting")
        if state == "on": self.start_btn.setText("停止服务")
        else: self.start_btn.setText("启动服务")
        self.chat.set_enabled(
            state == "on",
            "" if state == "on" else "启动服务后可发送消息")
        if state == "on" and not self._verified and not self._verify_busy:
            self._verify_self()
        if self.hub is not None and self.hub.error:
            self._write(f"hub 错误：{self.hub.error}")
            self.hub._error = None

    def _verify_self(self):
        """hub 起来了，从本机打一次 /api/status。

        这是把「界面说在线」和「对方真的连得上」分开：本机这一趟要是也过不去，
        问题在 hub 自己（密钥、网段、端口），跟网络对面无关。
        """
        self._verify_busy = True
        try:
            port = int(self.port.text())
        except ValueError:
            port = int(getattr(self.cfg, "port", 8765) or 8765)
        token = self.token.text().strip()
        self._write(f"本机自检：向 127.0.0.1:{port} 请求 /api/status…")
        _run_on_thread(
            lambda: linkcheck.check_link("127.0.0.1", port, token),
            lambda result, error: self._verify_bridge.done.emit(result, error))

    def _after_verify(self, report, error):
        self._verify_busy = False
        self._verified = True
        if self._closed: return
        if error is not None:
            self._write(f"本机自检未能启动：{error}")
            return
        for line in report.lines:
            self._write(f"  {line}")
        if report.ok:
            self._write("本机自检通过：hub 侧运行正常；若对端仍无法连接，请检查网络或允许网段。")
        else:
            self._write("本机自检未通过：上方标记 ✗ 的步骤即为对端无法连接的原因。")

    # -- 消息 ---------------------------------------------------------------
    def _chat_sink(self, agent_id: str, text: str, ts: float) -> None:
        """hub 的事件循环线程里调用——只允许 emit，绝不能碰控件。"""
        if not self._closed:
            self._chat_bridge.received.emit(agent_id, text, ts)

    def _on_chat_received(self, agent_id, text, ts):
        if self._closed:
            return
        self.chat.append(f"客户端（{agent_id}）", text, ts)
        if not _window_is_active(self.widget):
            _alert_window(self.widget.window())

    def _attach_chat(self):
        """把消息监听器挂到 hub 模块上。测试里的假 hub 没有这套接口，跳过即可。"""
        module = getattr(self.hub, "module", None)
        add = getattr(module, "add_chat_listener", None)
        if add is not None:
            add(self._chat_listener)

    def _detach_chat(self):
        module = getattr(self.hub, "module", None)
        remove = getattr(module, "remove_chat_listener", None)
        if remove is not None:
            remove(self._chat_listener)

    def _send_chat(self, text: str):
        if not self._hub_running():
            self.chat.system("服务尚未启动，消息未发送。")
            return
        if self._chat_busy:
            self.chat.system("上一条消息仍在发送，请稍候。")
            return
        try:
            port = int(self.port.text())
        except ValueError:
            port = int(getattr(self.cfg, "port", 8765) or 8765)
        token = self.token.text().strip()
        self._chat_busy = True
        # done 在**工作线程**里被调用，所以只能 emit，界面更新走 _after_chat_sent。
        _run_on_thread(
            lambda: _post_chat(port, token, text),
            lambda result, error: self._chat_bridge.sent.emit(text, result, error))

    def _after_chat_sent(self, text, result, error):
        self._chat_busy = False
        if self._closed:
            return
        if error is not None:
            self.chat.system(f"发送失败：{error}")
            return
        self.chat.append("服务器端", text)
        self.chat.clear_input()

    def close(self):
        self._closed = True
        self._detach_chat()
        if self.hub is not None and self.hub.running:
            self.hub.force_stop()


class ClientPage:
    def __init__(self, host, prefill: ConnInfo | None = None):
        QtCore, _, QtWidgets = _qt()
        self.host = host
        class _EventBridge(QtCore.QObject):
            event = QtCore.Signal(str, object)
        self._events = _EventBridge()
        self._events.event.connect(self._event)
        # 「检测」也跑在工作线程里（它要发 HTTP，不能卡住界面），
        # 所以回调只允许 emit —— 见 _run_on_thread 的说明。
        class _CheckBridge(QtCore.QObject):
            done = QtCore.Signal(object, object)
        self._check_bridge = _CheckBridge()
        self._check_bridge.done.connect(self._after_check)
        self._checking = False
        self._closed = False
        self.widget = QtWidgets.QWidget(objectName="page")
        root = QtWidgets.QVBoxLayout(self.widget)
        root.setContentsMargins(34, 30, 34, 24)
        root.setSpacing(16)
        root.addWidget(_label("客户端工作台", "pageTitle"))
        root.addWidget(_label("连接对端电脑，实时查看连接状态与 agent 事件。", "pageSub"))

        connect, form_layout = _card("连接", "粘贴连接串、命令行或聊天记录，将自动提取地址与密钥。")
        self.paste = QtWidgets.QLineEdit()
        self.paste.setPlaceholderText("ws://192.168.1.23:8765#密钥 或 --server ... --token ...")
        self.paste.returnPressed.connect(self._apply_paste)
        form_layout.addWidget(self.paste)
        line = QtWidgets.QHBoxLayout()
        line.setSpacing(10)
        self.host_edit = QtWidgets.QLineEdit()
        self.port_edit = QtWidgets.QLineEdit("8765")
        self.token_edit = QtWidgets.QLineEdit()
        self.token_edit.setEchoMode(QtWidgets.QLineEdit.Password)
        self.mode = QtWidgets.QComboBox()
        self.mode.addItem("确认（推荐）", "confirm")
        self.mode.addItem("只读（安全）", "safe")
        self.mode.addItem("自动执行", "auto")
        for title, widget in (("地址", self.host_edit), ("端口", self.port_edit), ("密钥", self.token_edit), ("策略", self.mode)):
            col = QtWidgets.QVBoxLayout(); col.setSpacing(4); col.addWidget(_label(title, "muted")); col.addWidget(widget); line.addLayout(col, 1)
        self.port_edit.setMaximumWidth(130)
        form_layout.addLayout(line)
        buttons = QtWidgets.QHBoxLayout()
        self.connect_btn = QtWidgets.QPushButton("连接")
        self.connect_btn.setObjectName("primary")
        self.connect_btn.clicked.connect(self._connect)
        self.disconnect_btn = QtWidgets.QPushButton("断开")
        self.disconnect_btn.clicked.connect(self._disconnect)
        self.disconnect_btn.setEnabled(False)
        self.check_btn = QtWidgets.QPushButton("检测")
        self.check_btn.setToolTip("不建立连接，仅执行一次探测：地址是否可达、密钥是否正确、是否会被允许网段拒绝。")
        self.check_btn.clicked.connect(self._check)
        self.parse_hint = _label("", "muted")
        buttons.addWidget(self.connect_btn); buttons.addWidget(self.check_btn); buttons.addWidget(self.disconnect_btn); buttons.addWidget(self.parse_hint); buttons.addStretch()
        form_layout.addLayout(buttons)
        root.addWidget(connect)

        status_row = QtWidgets.QHBoxLayout()
        self.status = _label("未连接", "badge"); self._set_status("off")
        self.session = _label("会话码  ------", "cardTitle")
        status_row.addWidget(self.status); status_row.addWidget(self.session); status_row.addStretch()
        root.addLayout(status_row)

        log_card, log_layout = _card("实时事件", "包含连接、重连及远端命令执行结果。")
        self.log = QtWidgets.QPlainTextEdit(); self.log.setReadOnly(True); self.log.setMinimumHeight(140)
        log_layout.addWidget(self.log)

        # 和服务器端页一样并排放：竖着摞会把输入框挤出默认窗口。
        self.chat = ChatPanel(self._send_chat)
        bottom = QtWidgets.QHBoxLayout()
        bottom.setSpacing(12)
        bottom.addWidget(self.chat.frame, 5)
        bottom.addWidget(log_card, 7)
        root.addLayout(bottom, 1)

        self.runner = None
        self._conn: ConnInfo | None = None
        self._mode_used = "confirm"
        if prefill is not None:
            self._fill(prefill)
            self.log.appendPlainText(
                f"已载入 {prefill.host}:{prefill.port}（密钥已隐藏）。"
                f"此为历史记录，不代表当前仍可连接；请先点击「检测」验证。")
        else:
            remembered = store.load()
            if remembered:
                self._fill(remembered.conn); self.mode.setCurrentIndex(max(0, self.mode.findData(remembered.mode)))
                # 以前这里是个**没有任何说明**的字符串：地址和密钥被默默填好，
                # 分不清是历史记录还是谁发来的样本。写一行出来。
                self.log.appendPlainText(
                    f"已载入上次连接的 {remembered.conn.host}:{remembered.conn.port}"
                    f"（密钥已隐藏）。请先点击「检测」确认其仍然有效。")

    def _set_status(self, state):
        values = {"on": ("已连接", "#dcfce7", "#15803d"), "wait": ("连接中", "#fef3c7", "#b45309"), "bad": ("连接异常", "#fee2e2", "#b91c1c"), "off": ("未连接", "#e2e8f0", "#475569")}
        text, bg, fg = values.get(state, values["off"]); self.status.setText(text); self.status.setStyleSheet(f"background:{bg};color:{fg};")

    def _fill(self, info: ConnInfo):
        self.host_edit.setText(info.host); self.port_edit.setText(str(info.port)); self.token_edit.setText(info.token); self.paste.setText(info.encode())

    def _apply_paste(self):
        try:
            info = parse(self.paste.text())
            self._fill(info); self.parse_hint.setText("已解析连接串"); self.parse_hint.setStyleSheet("color:#15803d")
        except ConnInfoError as exc:
            parts = extract(self.paste.text())
            if parts.host: self.host_edit.setText(parts.host)
            if parts.port: self.port_edit.setText(str(parts.port))
            if parts.token: self.token_edit.setText(parts.token)
            self.parse_hint.setText(str(exc)); self.parse_hint.setStyleSheet("color:#b91c1c")

    def _connect(self):
        try:
            info = ConnInfo(self.host_edit.text(), int(self.port_edit.text()), self.token_edit.text())
        except (ValueError, ConnInfoError) as exc:
            self.parse_hint.setText(str(exc)); self.parse_hint.setStyleSheet("color:#b91c1c"); return
        if self.runner is not None: return
        from agent.agent import default_audit_path
        QtCore, _, _ = _qt()          # 这个模块是懒加载 Qt 的，方法里得自己解包
        cfg = {"server_url": info.ws_url, "token": info.token, "mode": self.mode.currentData(),
               "audit_path": str(default_audit_path()), "ui": "gui"}
        self._conn = info
        self._mode_used = self.mode.currentData()
        self.runner = AgentRunner(cfg, QtCore)
        self.runner.on_event = lambda kind, fields: self._events.event.emit(kind, fields)
        self._set_status("wait"); self.connect_btn.setEnabled(False); self.disconnect_btn.setEnabled(True); self.log.appendPlainText(f"正在连接 {info.ws_url}…")
        self.runner.start()

    def _event(self, kind, fields):
        # 聊天消息只进消息区。必须在下面那条通用的 appendPlainText 之前返回，
        # 否则同一句话会同时出现在"实时事件"里。
        if kind == "chat":
            text = fields.get("text") or ""
            if text:
                self.chat.append("服务器端", text, fields.get("ts"))
                if not _window_is_active(self.widget):
                    _alert_window(self.widget.window())
            return

        text = fields.get("text") or fields.get("why") or ""
        if kind == "connected":
            self._set_status("on"); self.session.setText(f"会话码  {fields.get('session_code', '------')}")
            text = f"已连接 {fields.get('server', '')}"
            # 只有服务器端真的开着消息面板，输入框才点得亮。旧 hub 或
            # `python -m server.hub` 起的命令行 hub 不会带这个标记。
            chat_ok = fields.get("chat") is True
            self.chat.set_enabled(
                chat_ok,
                "" if chat_ok else "对端 hub 未开启消息面板（命令行版），不支持消息")
            # 只有握手成功才记住连接信息，输错密钥时不会污染“最近连接”。
            if self._conn is not None:
                try:
                    store.save(self._conn, self._mode_used)
                except OSError:
                    self.log.appendPlainText("（连接信息保存失败，下次需要重新粘贴）")
        elif kind == "disconnected":
            # Agent 会在每次短暂断线时发这个事件，之后可能继续重连；runner
            # 只有在线程真正结束时才能清掉。
            self._set_status("off")
            self.chat.set_enabled(False, "连接断开，等待重连后可继续发送")
        elif kind == "error":
            self._set_status("bad")
            self.chat.set_enabled(False, "连接异常，暂不能发送")
        elif kind == "fatal":
            self._set_status("bad")
            self.chat.set_enabled(False, "连接已终止，消息不可用")
        elif kind == "finished":
            self.runner = None
            self._conn = None
            self.session.setText("会话码  ------")
            self.connect_btn.setEnabled(True)
            self.disconnect_btn.setEnabled(False)
            self.chat.set_enabled(False, "未连接")
        elif kind == "reconnecting":
            text = f"连接断开，{fields.get('seconds', 0):.0f} 秒后重连"
            self.chat.set_enabled(False, "连接断开，等待重连后可继续发送")
        if kind == "cmd": text = f"远端命令：{fields.get('cmd', '')}"
        if text: self.log.appendPlainText(text)

    def _send_chat(self, text: str):
        runner = self.runner
        if runner is None or not runner.send_chat(text):
            self.chat.system("尚未连接，消息未发送。")
            return
        self.chat.append("客户端", text)
        self.chat.clear_input()

    def _check(self):
        """连之前先问清楚：地址通不通、密钥对不对、会不会被网段拒掉。

        只读，**不启动 agent**——所以没连接的时候随便点。
        """
        if self._checking: return
        try:
            info = ConnInfo(self.host_edit.text(), int(self.port_edit.text()), self.token_edit.text())
        except (ValueError, ConnInfoError) as exc:
            self.parse_hint.setText(str(exc)); self.parse_hint.setStyleSheet("color:#b91c1c")
            self.log.appendPlainText(f"检测未启动：{exc}"); return
        self._checking = True
        self.check_btn.setEnabled(False)
        self.parse_hint.setText("检测中…"); self.parse_hint.setStyleSheet("color:#64748b")
        # 这里只写 ws://地址:端口。密钥绝不进日志——日志会被贴到聊天里。
        self.log.appendPlainText(f"检测 {info.ws_url}（密钥已隐藏）…")
        _run_on_thread(
            lambda: linkcheck.check_link(info.host, info.port, info.token),
            lambda result, error: self._check_bridge.done.emit(result, error))

    def _after_check(self, report, error):
        # done 是在工作线程里调的，能到这里说明信号已经跨回界面线程了。
        self._checking = False
        if self._closed: return
        self.check_btn.setEnabled(True)
        if error is not None:
            self.parse_hint.setText(f"检测失败：{error}"); self.parse_hint.setStyleSheet("color:#b91c1c")
            self.log.appendPlainText(f"检测失败：{error}"); return
        for line in report.lines:
            self.log.appendPlainText(line)
        if report.ok:
            self.parse_hint.setText("检测通过"); self.parse_hint.setStyleSheet("color:#15803d")
        else:
            self.parse_hint.setText(f"检测未通过：{report.failed.text}")
            self.parse_hint.setStyleSheet("color:#b91c1c")

    def _disconnect(self):
        if self.runner is not None: self.runner.stop(); self.log.appendPlainText("正在断开…")

    def close(self):
        self._closed = True
        self.chat.set_enabled(False, "窗口正在关闭")
        if self.runner is not None: self.runner.stop()


class MainWindow:
    def __init__(self, args):
        QtCore, QtGui, QtWidgets = _qt()
        self.QtCore = QtCore
        self.args = args
        self.window = QtWidgets.QMainWindow()
        self.window.setWindowTitle("远程电脑助手")
        self.window.resize(1180, 760)
        self.window.setMinimumSize(960, 640)
        self.window.setStyleSheet(_stylesheet())
        self.window.closeEvent = self._close_event

        central = QtWidgets.QWidget(); self.window.setCentralWidget(central)
        layout = QtWidgets.QHBoxLayout(central); layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(0)
        side = QtWidgets.QFrame(objectName="sidebar"); side.setFixedWidth(224); sl = QtWidgets.QVBoxLayout(side); sl.setContentsMargins(18, 24, 18, 18); sl.setSpacing(8)
        sl.addWidget(_label("远程电脑助手", "brand")); sl.addWidget(_label("PRIVATE REMOTE WORKSPACE", "brandSub")); sl.addSpacing(26)
        self.nav = {}
        for key, text in (("home", "⌂  概览"), ("server", "▣  服务器端"), ("client", "↔  客户端")):
            b = QtWidgets.QPushButton(text, objectName="nav"); b.setProperty("active", False); b.clicked.connect(lambda _, k=key: self.show_page(k)); sl.addWidget(b); self.nav[key] = b
        sl.addStretch(); sl.addWidget(_label("连接状态将持续显示在工作台中。\n关闭窗口将终止当前会话。", "brandSub"))
        layout.addWidget(side)
        self.stack = QtWidgets.QStackedWidget(); layout.addWidget(self.stack, 1)
        self.pages = {}
        self._page_hosts = {}
        self.pages["home"] = WelcomePage(self).widget
        self._add_page("home", self.pages["home"])
        self.show_page("home")

        role = getattr(args, "role", None)
        if role in {"server", "client"}: self.show_page(role)
        if role == "client":
            page = self.pages["client"]
            mode = getattr(args, "mode", None)
            if mode:
                mode_index = page.mode.findData(mode)
                if mode_index >= 0:
                    page.mode.setCurrentIndex(mode_index)

            # GUI 参数沿用命令行支持的 ws://host:port 形式。直接把整串交给
            # ConnInfo 会得到 `ws://ws://...:8765`，这里复用同一解析器拆字段，
            # 也保留“只给地址/只给密钥时先填已有部分”的体验。
            raw_server = getattr(args, "server", None)
            raw_token = getattr(args, "token", None)
            if raw_server or raw_token:
                parts = extract(f"--server {raw_server or ''} --token {raw_token or ''}")
                if parts.host:
                    page.host_edit.setText(parts.host)
                if parts.port:
                    page.port_edit.setText(str(parts.port))
                if parts.token:
                    page.token_edit.setText(parts.token)
                page.paste.setText(raw_server or "")

    def show_page(self, key: str, info: ConnInfo | None = None):
        # 按需创建角色页面：客户端启动时不加载 server.config，配置文件损坏也
        # 不会阻塞客户端这一侧。
        if key == "server" and key not in self.pages:
            self.pages[key] = ServerPage(self, config_path=getattr(self.args, "config", None))
            self._add_page(key, self.pages[key].widget)
        elif key == "client" and key not in self.pages:
            self.pages[key] = ClientPage(self)
            self._add_page(key, self.pages[key].widget)
        if key == "client" and info is not None:
            page = self.pages.get("client")
            if page: page._fill(info)
        page_widget = self.pages.get(key)
        if page_widget is not None:
            self.stack.setCurrentWidget(self._page_hosts[key])
        for name, button in self.nav.items(): button.setProperty("active", name == key); button.style().unpolish(button); button.style().polish(button)

    def _add_page(self, key, page_widget):
        """把页面放进滚动容器，窄窗口下字段不会被直接裁切。"""
        QtCore, _, QtWidgets = _qt()
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        page_widget.setMinimumWidth(720 if key != "home" else 0)
        scroll.setWidget(page_widget)
        self._page_hosts[key] = scroll
        self.stack.addWidget(scroll)

    def _close_event(self, event):
        for page in self.pages.values():
            if hasattr(page, "close"): page.close()
        event.accept()


def run_pyside(args) -> int:
    """启动 PySide6 界面。"""
    _, QtGui, QtWidgets = _qt()
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    app.setStyle("Fusion")
    # 任务栏和窗口左上角用的图标。exe 自己的图标（--icon）管的是资源管理器里的
    # 样子，窗口图标得单独设；打包时 assets\ 跟 exe 一起走（tools/build_app.py）。
    icon = paths.asset_path("remote-pc.ico")
    if icon.is_file():
        app.setWindowIcon(QtGui.QIcon(str(icon)))
    window = MainWindow(args)
    window.window.show()
    return app.exec()


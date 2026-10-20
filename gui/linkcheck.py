"""连上之前先把"哪一步断了"问清楚。

为什么要有这个：客户端连不上 hub 时，能看到的只有 agent 那一句笼统的报错。
可真实原因是**好几种**完全不同的东西——端口没人听、密钥不对、本机地址不在
服务器的 `allowed_networks` 里（hub 在握手**之前**就 `close(4403)`，对方那边
看起来和"地址写错"一模一样）、服务器端压根没配 token、那个端口上跑的根本
是别的服务。每一种都只报"连不上"，人就只能一个一个猜。

所以这里把一次连接拆成四步，逐条给结论，每一步失败都附一句"照着做"：

    ✓ TCP 8765 可达
    ✓ 密钥正确（/api/status 返回 200）
    ✗ 本机地址 100.64.3.7 不在服务器的 allowed_networks 内
       → 从这台机器连过去会被 4403 拒绝
    在线 1 台  会话码 045387

第 ③ 步要读**服务器端**的 `allowed_networks`，所以 `/api/status` 里加了那个字段。
它本来就是给本机客户端看的（`server/cli.py status`、MCP 的 `remote_status`），
多带一个自己配的网段不算新的暴露。

这个模块**不碰 Qt**：探测函数和源地址函数都可以注入，界面那一层只负责把它画出来。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .conninfo import DEFAULT_PORT
from .netinfo import address_allowed, source_ip_for

# 每一步的三种状态。INFO 是"这一步没结论"——比如服务器端没报告网段，
# 或者本机出口地址探测不出来。它**不算失败**，但也不该打勾说"检查过了"。
OK = "ok"
BAD = "bad"
INFO = "info"

_MARKS = {OK: "✓", BAD: "✗", INFO: "•"}

PROBE_OK = "ok"
PROBE_UNREACHABLE = "unreachable"      # 连不上：没监听 / 地址错 / 防火墙
PROBE_DENIED = "denied"                # 401：密钥不对
PROBE_UNCONFIGURED = "unconfigured"    # 500：服务器端没配 token
PROBE_OTHER = "other"                  # 通了，但那个端口上不是我们的 hub


@dataclass(frozen=True)
class Step:
    state: str
    text: str
    hint: str = ""


@dataclass(frozen=True)
class Probe:
    """一次 `/api/status` 探测的结果。

    `status` 为 None 时 `kind`/`reason` 说明为什么；成功时 `kind` 是 PROBE_OK。
    """

    status: dict[str, Any] | None = None
    kind: str = PROBE_OK
    reason: str = ""


@dataclass(frozen=True)
class LinkReport:
    steps: tuple[Step, ...]
    summary: str = ""

    @property
    def ok(self) -> bool:
        """有没有硬失败。INFO 不算。"""
        return all(step.state != BAD for step in self.steps)

    @property
    def failed(self) -> Step | None:
        return next((s for s in self.steps if s.state == BAD), None)

    @property
    def lines(self) -> list[str]:
        out: list[str] = []
        for step in self.steps:
            out.append(f"{_MARKS.get(step.state, '?')} {step.text}")
            if step.hint:
                out.append(f"   → {step.hint}")
        if self.summary:
            out.append(self.summary)
        return out


def default_probe(host: str, port: int, token: str, timeout_s: float) -> Probe:
    """真的去打一次 `/api/status`。复用 `server.remote.RemoteClient`。

    `RemoteClient._call` 已经把连不上翻译成人话（见那边的 ConnectTimeout /
    ConnectError 分支），把 4xx/5xx 翻成带 `status` 的 `RemoteError`。
    这里只做分流，不重写一套 HTTP。
    """
    from server.config import ServerConfig
    from server.remote import RemoteClient, RemoteError

    # 目标地址来自界面字段，不是本机那份 config.toml——所以临时构一个
    # ServerConfig 指过去，而不是让 RemoteClient 自己去 load_config()。
    cfg = ServerConfig(token=token, port=port, hub_url=f"http://{host}:{port}")
    try:
        with RemoteClient(cfg, timeout_s=timeout_s) as client:
            return Probe(status=client.status())
    except RemoteError as exc:
        text = str(exc)
        code = exc.status
        if code == 401:
            return Probe(kind=PROBE_DENIED, reason=text)
        if code == 500:
            return Probe(kind=PROBE_UNCONFIGURED, reason=text)
        if code is None:
            # 没有状态码 = 一个 HTTP 响应都没收到（连接被拒 / 超时 / 防火墙）
            return Probe(kind=PROBE_UNREACHABLE, reason=text)
        # 收到了响应，但不是 200/401/500——多半这个端口上压根是别的服务。
        return Probe(kind=PROBE_OTHER, reason=text)
    except Exception as exc:  # noqa: BLE001 - 界面要的是"为什么"，不是异常类型
        return Probe(kind=PROBE_UNREACHABLE, reason=str(exc))


def check_link(
    host: str,
    port: int = DEFAULT_PORT,
    token: str = "",
    *,
    timeout_s: float = 5.0,
    probe: Callable[[str, int, str, float], Probe] | None = None,
    source_ip: Callable[[str, int], str | None] | None = None,
) -> LinkReport:
    """逐项检查"我这台机器能不能连上这个 hub"。**不抛异常**，全部变成 Step。"""
    probe = probe or default_probe
    source_ip = source_ip or source_ip_for

    host = (host or "").strip()
    try:
        port_num = int(port)
    except (TypeError, ValueError):
        return LinkReport((Step(BAD, f"端口 {port!r} 不是有效数字",
                                "端口应为 1–65535 之间的整数"),))
    if not host:
        return LinkReport((Step(BAD, "未填写服务器地址",
                                "请从服务器端复制完整连接串后粘贴"),))
    if not 1 <= port_num <= 65535:
        return LinkReport((Step(BAD, f"端口 {port_num} 超出 1–65535 范围"),))
    if not token.strip():
        return LinkReport((Step(BAD, "未填写密钥",
                                "连接串格式为 ws://地址:端口#密钥，"
                                "请从服务器端复制完整连接串"),))

    steps: list[Step] = []

    # ② hub 可达 + 密钥正确。这两条是从同一次探测里读出来的，所以下面按
    #    结果分别落成两行——"端口通不通"和"密钥对不对"是两件不同的事，
    #    合成一句"连不上"就又回到改这个模块之前了。
    result = probe(host, port_num, token, timeout_s)
    if result.kind == PROBE_UNREACHABLE:
        # 连不上有好几种形态（拒绝 / 超时 / DNS 查不到），RemoteClient 那边
        # 已经把它们分别翻成人话了——原样带出来，别在这儿又糊成一句"连不上"。
        hint = ("服务器端未启动服务，或地址/端口有误，"
                "也可能被防火墙拦截")
        if result.reason:
            hint += f"。详细原因：{result.reason}"
        steps.append(Step(BAD, f"TCP {port_num} 无法连接", hint))
        return LinkReport(tuple(steps))
    if result.kind == PROBE_UNCONFIGURED:
        steps.append(Step(OK, f"TCP {port_num} 可达"))
        steps.append(Step(BAD, "服务器端未配置 token",
                          "请在服务器端运行 tools/gen_token.py，或在工作台中"
                          "点击「随机生成密钥」"))
        return LinkReport(tuple(steps))
    if result.kind == PROBE_DENIED:
        steps.append(Step(OK, f"TCP {port_num} 可达"))
        steps.append(Step(BAD, "密钥不正确（/api/status 返回 401）",
                          "两端必须使用同一密钥。请从服务器端「连接信息」"
                          "重新复制完整连接串，或重新生成密钥后重启两端"))
        return LinkReport(tuple(steps))
    if result.kind == PROBE_OTHER:
        # 端口通了、HTTP 也应答了，但那不是我们的 hub——最常见的是端口被
        # 别的程序占着（很多开发工具默认就爱用 8xxx）。这时候再往下看密钥、
        # 网段都是白费，直接说清楚。
        steps.append(Step(OK, f"TCP {port_num} 可达"))
        steps.append(Step(BAD, "该端口上存在其他服务，并非本 hub",
                          f"请更换空闲端口，或先关闭占用 {port_num} 的程序。"
                          f"（{result.reason}）"))
        return LinkReport(tuple(steps))

    status = result.status or {}
    steps.append(Step(OK, f"TCP {port_num} 可达"))
    steps.append(Step(OK, "密钥正确（/api/status 返回 200）"))

    # ③ 网段：这才是"端口通、密钥对，可就是连不上"的真凶。
    #
    # 注意 `None` 和 `[]` 是两码事，不能并成 `if not networks`：
    #   None = 键都没有 —— 服务器端版本旧，这一项没结论，跳过（INFO）
    #   []   = 键在、值是空 —— hub 的 `_client_allowed` 循环一次都不进，
    #          直接 `return False`，**所有人都被 4403 拒掉**。这是硬失败。
    networks = status.get("allowed_networks")
    if networks is None:
        steps.append(Step(INFO, "服务器端未报告 allowed_networks，已跳过网段检查",
                          "服务器端版本较旧；升级后方可判断此项"))
    elif not networks:
        steps.append(Step(
            BAD, "服务器端的 allowed_networks 为空",
            "空列表表示拒绝所有连接。请在服务器端 config.toml 的 [server] 中"
            "填写允许网段（例如 allowed_networks = [\"192.168.0.0/16\"]），然后重启服务"))
    else:
        src = source_ip(host, port_num)
        if not src:
            steps.append(Step(INFO, "无法探测本机出口地址，已跳过网段检查"))
        elif address_allowed(src, networks):
            steps.append(Step(OK, f"本机地址 {src} 位于服务器允许的网段内"))
        else:
            steps.append(Step(
                BAD, f"本机地址 {src} 不在服务器允许的网段内",
                f"hub 将在握手前以 4403 拒绝此连接。请将 {src} 加入"
                f"服务器端的 allowed_networks（当前为 "
                f"{'、'.join(str(n) for n in networks)}），或改用同一网段的网卡"))

    # ④ 在线设备：这条**不是**连接能不能成的判据——客户端自己就是要连过去的
    #    那台 agent，此刻没有别的 agent 在线是正常的。
    agents = status.get("agents") or []
    if agents:
        codes = "、".join(str(a.get("session_code") or "------") for a in agents)
        summary = f"在线 {len(agents)} 台  会话码 {codes}"
    else:
        summary = "在线 0 台：对端 agent 尚未连接"

    return LinkReport(tuple(steps), summary)

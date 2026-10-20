"""本机局域网 IPv4 探测。

服务器端窗口要大字显示"对方该连哪个地址"。这套判断以前散在 tools/gen_token.py 里，
搬过来之后做成可注入的纯函数，好在没有网卡的测试环境里验证过滤和排序规则。
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Iterable

from .conninfo import DEFAULT_PORT

# UDP connect 不会真的发包，只是让内核挑出"走默认路由的那个网卡"。
# 这个地址也就是对方最可能连得上的那个。
DEFAULT_PROBE_TARGET = ("8.8.8.8", 80)


def is_usable(ip: str) -> bool:
    """这个地址能不能写在"让对方连过来"的位置上。"""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if addr.version != 4:
        return False
    # 三种都不行：
    #   loopback   127.x —— 只有本机能用
    #   link_local 169.254.x —— 没拿到 DHCP 时网卡自封的地址，对方永远连不上
    #   unspecified 0.0.0.0 —— 是个占位符，不是一个能连的地址
    return not (addr.is_loopback or addr.is_link_local or addr.is_unspecified)


def _real_getaddrinfo() -> list[str]:
    try:
        return [info[4][0]
                for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)]
    except OSError:
        return []


def _real_udp_probe() -> str | None:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except OSError:
        return None
    try:
        sock.connect(DEFAULT_PROBE_TARGET)
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


def list_lan_ipv4(
    *,
    getaddrinfo: Callable[[], Iterable[str]] | None = None,
    udp_probe: Callable[[], str | None] | None = None,
) -> list[str]:
    """本机可用的局域网 IPv4，默认出口网卡的那个排最前。

    两个参数是给测试注入的，默认用真实实现。排序按数值而不是字符串，
    否则 192.168.1.10 会排到 192.168.1.9 前面。
    """
    getaddrinfo = getaddrinfo or _real_getaddrinfo
    udp_probe = udp_probe or _real_udp_probe

    found = {ip for ip in getaddrinfo() if ip}
    usable = sorted((ip for ip in found if is_usable(ip)),
                    key=lambda s: ipaddress.IPv4Address(s))

    probe = udp_probe()
    if probe and is_usable(probe):
        # 顺便把探测到的地址补进来：有的机器 gethostname() 解析不出网卡地址
        # （DNS 或 hosts 里没有这台机器的名字），只靠 getaddrinfo 会一无所获。
        usable = [probe] + [ip for ip in usable if ip != probe]
    return usable


def pick_default(ips: list[str]) -> str | None:
    return ips[0] if ips else None


def _real_connect(host: str, port: int) -> str | None:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except OSError:
        return None
    try:
        sock.connect((host, port))
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


def source_ip_for(host: str, port: int = DEFAULT_PORT, *,
                  connect: Callable[[str, int], str | None] | None = None) -> str | None:
    """本机连到 `host:port` 时会用哪个源地址。拿不到就 None。

    这就是**对方 hub 看到的那个 IP**——`server/hub.py` 的 `_client_allowed`
    判的正是它（`ws.client.host`）。所以"我这台机器会不会被 4403 拒掉"
    这个问题，答案就是 `address_allowed(source_ip_for(hub), allowed_networks)`。

    同样用 UDP `connect()`：不会真发包，只是让内核按路由表挑一张网卡出来。
    TCP 连一个没人监听的端口会先 SYN 再超时，正是要避免的慢。
    """
    connect = connect or _real_connect
    try:
        ip = connect(host, port)
    except Exception:  # noqa: BLE001 - 探测失败一律当"不知道"
        return None
    return ip or None


def address_allowed(ip: str, networks: Iterable[str]) -> bool:
    """这个地址在不在 allowed_networks 里。语义和 hub._client_allowed 一致。

    单独拎出来是因为这是最容易白折腾半天的故障：地址落在允许网段之外时，
    hub 会直接 4403 关掉连接，对方那边只看到"连不上"，什么都说明不了。
    界面必须替他问出这句话。
    """
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for net in networks:
        try:
            if addr in ipaddress.ip_network(net, strict=False):
                return True
        except ValueError:
            continue          # 配置里写坏的网段，跳过（hub 那边也只是 warning）
    return False

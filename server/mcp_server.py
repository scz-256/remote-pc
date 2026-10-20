"""把远程终端包装成 MCP 工具，供 Claude Code 调用。

注册（在项目目录下执行一次）：
    claude mcp add remote-pc -- python -m server.mcp_server

之后在 CC 里就能直接说"在他电脑上跑一下 ipconfig"。

注意：这个进程走 stdio 跟 CC 说话，**stdout 是协议通道**，
所以任何调试输出都必须走 stderr。
"""

from __future__ import annotations

import sys
from typing import Any

from mcp.server.fastmcp import FastMCP

from .remote import RemoteClient, RemoteError

mcp = FastMCP("remote-pc")

_client: RemoteClient | None = None


def client() -> RemoteClient:
    global _client
    if _client is None:
        _client = RemoteClient()
    return _client


def _guard(fn, fmt=lambda v: str(v)) -> str:
    """跑一个远端调用并格式化成给 CC 看的文本。

    异常一律转成可读字符串——MCP 工具抛异常会让整轮对话难看，
    而且 CC 从错误文本里能自己决定要不要重试。
    """
    try:
        value = fn()
    except RemoteError as exc:
        return f"失败：{exc}"
    except Exception as exc:  # 兜底，避免 MCP 工具异常把会话弄崩
        return f"意外错误：{exc!r}"
    return fmt(value)


def _format(res: dict[str, Any]) -> str:
    lines: list[str] = []

    if res.get("denied"):
        lines.append(f"对方侧拒绝执行：{res['denied']}")
    if res.get("timed_out"):
        lines.append(
            f"命令在超时前没结束，仍在后台跑（session: {res.get('session')}）。"
            f"把下面那行的 seq 值传给 remote_session_output 的 since_seq，"
            f"就只看这之后的新输出（不传的话会把该会话的全部历史都拿回来）；"
            f"也可以直接 remote_kill 结束它。")

    stdout = (res.get("stdout") or "").rstrip("\n")
    stderr = (res.get("stderr") or "").rstrip("\n")
    notes = (res.get("notes") or "").strip()

    if stdout:
        lines.append("--- stdout ---")
        lines.append(stdout)
    if stderr:
        lines.append("--- stderr ---")
        lines.append(stderr)
    if not stdout and not stderr:
        lines.append("（没有输出）")
    if notes:
        lines.append(notes)

    meta = []
    if res.get("exit_code") is not None:
        meta.append(f"exit={res['exit_code']}")
    if res.get("running"):
        meta.append("running")
    if res.get("cwd"):
        meta.append(f"cwd={res['cwd']}")
    if res.get("shell"):
        meta.append(f"shell={res['shell']}")
    # 输出序号。下次只想看增量时，把这个值传给 remote_session_output 的 since_seq，
    # 否则会连该会话之前几条命令的输出一起拿回来。
    if res.get("last_seq") is not None:
        meta.append(f"seq={res['last_seq']}")
    if meta:
        lines.append("--- " + "  ".join(meta))

    return "\n".join(lines)


@mcp.tool()
def remote_status() -> str:
    """查看被控电脑是否在线、主机信息、当前工作目录和会话状态。

    在做任何操作前先调这个确认对方在线。
    """
    def go() -> str:
        st = client().status()
        if not st["agents"]:
            return ("没有 agent 在线。需要对方在他自己的电脑上运行 agent.py 并连到本机 hub。\n"
                    f"hub 当前模式：{st['mode']}")
        out = [f"hub 模式：{st['mode']}"]
        for ag in st["agents"]:
            info = ag.get("info", {})
            out.append(
                f"\n● {ag['agent']}（会话码 {ag.get('session_code')}，"
                f"已连接 {ag['age_s']}s）\n"
                f"  用户：{info.get('user')}@{info.get('hostname')}\n"
                f"  系统：{info.get('os')} {info.get('arch')}\n"
                f"  对端 IP：{info.get('peer')}\n"
                f"  策略模式：{info.get('mode')}   默认 shell：{info.get('shell')}"
            )
            sessions = ag.get("sessions", [])
            if sessions:
                out.append("  会话：")
                for s in sessions:
                    state = "运行中" if s["running"] else "空闲"
                    out.append(f"    - {s['session']}：{state}，cwd={s['cwd']}")
            else:
                out.append("  会话：（还没有，用 remote_exec 指定 session 名会自动建）")
        return "\n".join(out)

    return _guard(go)


@mcp.tool()
def remote_exec(cmd: str, session: str = "main", cwd: str | None = None,
                shell: str | None = None, timeout_s: float = 120.0,
                agent: str | None = None) -> str:
    """在对方的电脑上执行一条命令，返回它的输出。

    Args:
        cmd: 要执行的命令。默认是 PowerShell 语法。
        session: 会话名。同一个会话里工作目录会保持（cd 之后下一条命令还在那儿），
                 并且同一时刻只能跑一条命令。长任务请用单独的 session，
                 比如 "build"，这样不挡着别的操作。
        cwd: 执行前先切到这个目录；不填就沿用该会话上次的目录。
        shell: powershell（默认）/ pwsh / cmd / bash。
        timeout_s: 最多等多久。超时不会杀掉命令，它会继续在后台跑，
                   可以用 remote_session_output 续看输出。
        agent: 多台被控机在线时用来指定目标。

    注意：删除、移动、安装软件、关机之类的命令会触发对方屏幕上的确认框，
    对方点了"拒绝"就会返回"对方侧拒绝执行"。
    """
    return _guard(lambda: client().exec(
        cmd, agent=agent, session=session, cwd=cwd, shell=shell,
        timeout_s=timeout_s), _format)


@mcp.tool()
def remote_session_output(session: str = "main", since_seq: int = 0,
                          agent: str | None = None) -> str:
    """读取某个会话当前的输出和状态，不新起命令。

    **一定要传 since_seq**：不传（=0）会把该会话从建起到现在的所有输出
    一起返回，包括之前几条命令的。这个值就是上一次 remote_exec /
    remote_session_output 结果里那行的 `seq=N`，传进去就只看它之后的新输出。
    """
    return _guard(lambda: client().sessions(agent=agent, session=session,
                                            since_seq=since_seq), _format)


@mcp.tool()
def remote_write_stdin(data: str, session: str = "main",
                       agent: str | None = None) -> str:
    """往正在运行的命令写标准输入。

    用于需要交互的命令（比如 python 脚本里的 input()、安装程序的 y/n 确认）。
    记得带上换行符："y\\n"。
    """
    def go() -> str:
        client().stdin(data, agent=agent, session=session)
        return f"已写入 {len(data)} 个字符到 {session} 的标准输入。"
    return _guard(go)


@mcp.tool()
def remote_interrupt(session: str = "main", agent: str | None = None) -> str:
    """中断当前正在运行的命令（相当于在对面按 Ctrl+Break）。"""
    def go() -> str:
        client().signal(agent=agent, session=session, sig="ctrl-c")
        return f"已向 {session} 发送中断。"
    return _guard(go)


@mcp.tool()
def remote_kill(session: str = "main", agent: str | None = None) -> str:
    """强杀当前会话里正在运行的命令，连同它的子进程一起。"""
    def go() -> str:
        client().kill(agent=agent, session=session)
        return f"已强制结束 {session} 里的命令（含子进程）。"
    return _guard(go)


@mcp.tool()
def remote_put_file(local_path: str, remote_path: str,
                    agent: str | None = None) -> str:
    """把本机的文件传到对方电脑上（自动分片 + sha256 校验）。

    Args:
        local_path: 本机文件路径。
        remote_path: 对面的目标路径。如果对方设了目录限制（--allow-root），
                     必须落在他允许的范围内，否则会被拒绝。
    """
    def go() -> str:
        res = client().put_file(local_path, remote_path, agent=agent)
        return (f"已上传 {res['size']} 字节 -> {res['remote_path']}\n"
                f"sha256 {res['sha256']}")
    return _guard(go)


@mcp.tool()
def remote_get_file(remote_path: str, local_path: str,
                    agent: str | None = None) -> str:
    """把对方电脑上的文件取回本机（自动分片 + sha256 校验）。"""
    def go() -> str:
        res = client().get_file(remote_path, local_path, agent=agent)
        return f"已下载 {res['size']} 字节 -> {res['local_path']}"
    return _guard(go)


@mcp.tool()
def remote_screenshot(save_to: str | None = None, agent: str | None = None) -> str:
    """截取对方屏幕全画面，保存到本机并返回路径。

    用于确认对方机器上的图形界面状态（比如某个程序是不是真的弹出来了）。
    不填 save_to 就存到项目 logs/ 目录下。
    """
    def go() -> str:
        res = client().screenshot(agent=agent, save_to=save_to)
        return f"截屏已保存：{res['local_path']}（{res['size']} 字节）"
    return _guard(go)


@mcp.tool()
def remote_clipboard_get(agent: str | None = None) -> str:
    """读取对方电脑剪贴板里的文本。"""
    return _guard(lambda: client().clipboard_get(agent=agent)["text"] or "（剪贴板是空的）")


@mcp.tool()
def remote_clipboard_set(text: str, agent: str | None = None) -> str:
    """把文本写进对方电脑的剪贴板。"""
    def go() -> str:
        client().clipboard_set(text, agent=agent)
        return f"已写入对方剪贴板（{len(text)} 字）"
    return _guard(go)


def main() -> None:
    print("remote-pc MCP server 启动（stdio）", file=sys.stderr)
    mcp.run()


if __name__ == "__main__":
    main()

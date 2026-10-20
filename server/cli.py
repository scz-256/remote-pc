"""命令行调试工具。MCP 接好之前（和出问题时）用它验证链路。

    python -m server.cli status
    python -m server.cli exec "ipconfig" --session main
    python -m server.cli stdin main "y\n"
    python -m server.cli kill main
    python -m server.cli put ./setup.ps1 C:\\Users\\me\\setup.ps1
    python -m server.cli shot
"""

from __future__ import annotations

import argparse
import json
import sys

from .remote import RemoteClient, RemoteError


def _print_result(res: dict, *, max_chars: int = 200_000) -> int:
    stdout = res.get("stdout") or ""
    stderr = res.get("stderr") or ""
    notes = res.get("notes") or ""

    if res.get("denied"):
        print(f"\n\033[31m被拒绝：{res['denied']}\033[0m", file=sys.stderr)

    if stdout:
        sys.stdout.write(stdout if len(stdout) <= max_chars
                         else stdout[:max_chars] + "\n…（输出过长已截断）\n")
        if not stdout.endswith("\n"):
            sys.stdout.write("\n")
    if stderr:
        sys.stderr.write(stderr if len(stderr) <= max_chars
                         else stderr[:max_chars] + "\n…（已截断）\n")
    if notes:
        sys.stderr.write(notes)

    bits = []
    if res.get("exit_code") is not None:
        bits.append(f"退出码 {res['exit_code']}")
    if res.get("cwd"):
        bits.append(f"cwd {res['cwd']}")
    if res.get("running"):
        bits.append("仍在运行")
    if res.get("timed_out"):
        bits.append("超时")
    if bits:
        print(f"\n\033[90m[{res.get('session')}] {'  '.join(bits)}\033[0m", file=sys.stderr)

    if res.get("denied"):
        return 3
    if res.get("exit_code") is None:
        return 2
    return int(res["exit_code"])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m server.cli", description="远程终端命令行")
    ap.add_argument("--agent", help="指定 agent（多台在线时必填）")
    sub = ap.add_subparsers(dest="action", required=True)

    sub.add_parser("status", help="查看 agent 在线状态、会话码及各 session 状态")

    p_exec = sub.add_parser("exec", help="执行一条命令")
    p_exec.add_argument("cmd")
    p_exec.add_argument("--session", default="main")
    p_exec.add_argument("--cwd")
    p_exec.add_argument("--shell", choices=["powershell", "pwsh", "cmd", "bash"])
    p_exec.add_argument("--timeout", type=float, default=120.0)
    p_exec.add_argument("--json", action="store_true", help="输出原始 JSON")

    p_out = sub.add_parser("output", help="查看指定 session 的当前输出")
    p_out.add_argument("session", nargs="?", default="main")
    p_out.add_argument("--since", type=int, default=0)
    p_out.add_argument("--json", action="store_true")

    p_in = sub.add_parser("stdin", help="向运行中的命令写入标准输入")
    p_in.add_argument("session", nargs="?", default="main")
    p_in.add_argument("data")

    p_int = sub.add_parser("interrupt", help="中断（相当于 Ctrl+Break）")
    p_int.add_argument("session", nargs="?", default="main")

    p_kill = sub.add_parser("kill", help="强制终止运行中的命令（含子进程）")
    p_kill.add_argument("session", nargs="?", default="main")

    p_put = sub.add_parser("put", help="上传文件到对端")
    p_put.add_argument("local")
    p_put.add_argument("remote")

    p_get = sub.add_parser("get", help="从对端下载文件")
    p_get.add_argument("remote")
    p_get.add_argument("local")

    p_shot = sub.add_parser("shot", help="截屏并保存到本地")
    p_shot.add_argument("--save-to")

    p_clipg = sub.add_parser("clip-get", help="读取对端剪贴板")
    p_clips = sub.add_parser("clip-set", help="写入对端剪贴板")
    p_clips.add_argument("text")

    sub.add_parser("ping", help="探活")

    args = ap.parse_args(argv)

    try:
        with RemoteClient() as client:
            return _dispatch(client, args)
    except RemoteError as exc:
        print(f"\033[31m{exc}\033[0m", file=sys.stderr)
        return 1


def _dispatch(client: RemoteClient, args) -> int:  # noqa: C901
    a = args.action

    if a == "status":
        st = client.status()
        print(f"hub 模式：{st['mode']}    协议版本：{st['proto_version']}")
        if not st["agents"]:
            print("当前没有在线 agent。请在对方电脑上运行 agent.py 连接。")
            return 2
        for ag in st["agents"]:
            info = ag.get("info", {})
            print(f"\n● {ag['agent']}  会话码 {ag.get('session_code')}  "
                  f"已连接 {ag['age_s']}s")
            print(f"  {info.get('user')}@{info.get('hostname')}  "
                  f"{info.get('os')} {info.get('arch')}")
            print(f"  对端 IP {info.get('peer')}   模式 {info.get('mode')}   "
                  f"shell {info.get('shell')}")
            for sess in ag.get("sessions", []):
                state = "运行中" if sess["running"] else "空闲"
                print(f"    · {sess['session']}: {state}  cwd={sess['cwd']}")
        return 0

    if a == "exec":
        res = client.exec(args.cmd, agent=args.agent, session=args.session,
                          cwd=args.cwd, shell=args.shell, timeout_s=args.timeout)
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0
        return _print_result(res)

    if a == "output":
        res = client.sessions(agent=args.agent, session=args.session,
                              since_seq=args.since)
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0
        return _print_result(res)

    if a == "stdin":
        client.stdin(args.data, agent=args.agent, session=args.session)
        print("已送达", file=sys.stderr)
        return 0

    if a == "interrupt":
        client.signal(agent=args.agent, session=args.session, sig="ctrl-c")
        print("已发送中断", file=sys.stderr)
        return 0

    if a == "kill":
        client.kill(agent=args.agent, session=args.session)
        print("已强制终止", file=sys.stderr)
        return 0

    if a == "put":
        res = client.put_file(args.local, args.remote, agent=args.agent)
        print(f"已上传 {res['size']} 字节 -> {res['remote_path']}")
        print(f"sha256 {res['sha256']}")
        return 0

    if a == "get":
        res = client.get_file(args.remote, args.local, agent=args.agent)
        print(f"已下载 {res['size']} 字节 -> {res['local_path']}")
        return 0

    if a == "shot":
        res = client.screenshot(agent=args.agent, save_to=args.save_to)
        print(f"已保存 {res['local_path']}（{res['size']} 字节）")
        return 0

    if a == "clip-get":
        print(client.clipboard_get(agent=args.agent)["text"], end="")
        return 0

    if a == "clip-set":
        client.clipboard_set(args.text, agent=args.agent)
        print("已写入对端剪贴板", file=sys.stderr)
        return 0

    if a == "ping":
        print(json.dumps(client.ping(agent=args.agent), ensure_ascii=False))
        return 0

    raise SystemExit(f"未知操作：{a}")


if __name__ == "__main__":
    raise SystemExit(main())

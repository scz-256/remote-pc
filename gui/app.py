"""应用入口：命令行 agent 与唯一的 PySide6 图形界面。"""

from __future__ import annotations

import argparse
import ctypes
import os
import sys
from dataclasses import dataclass

from .conninfo import DEFAULT_PORT, Parts, extract

PROG = "remote-pc"
APP_VERSION = "0.1.0"          # 安装包用它填版本号，别在 .iss 里另写一个
CONSOLE_TITLE = "远程电脑助手 — 关闭此窗口即断开"
ROLES = ("server", "client")
AGENT_FLAGS = ("--server", "--token", "--config", "--mode", "--allow-root",
               "--shell", "--max-lifetime", "--confirm-timeout", "--name")
HELP_FLAGS = ("-h", "--help")

BANNER = """\
====================================================================
  远程电脑助手

  · 图形界面：不带参数运行，进入 PySide6 工作台
  · 命令行  ：带 --server/--token 等参数运行，作为命令行客户端
  · 关闭此窗口或图形界面即断开连接
===================================================================="""

HELP_NOTE = """\
（以上为命令行客户端的用法。图形界面为 PySide6 工作台，
  支持 --role server / --role client。）"""


def _flag_seen(argv: list[str], flag: str) -> bool:
    return any(a == flag or a.startswith(flag + "=") for a in argv)


def wants_agent_cli(argv: list[str]) -> bool:
    if _flag_seen(argv, "--init-config"):
        # 安装包在 `[Run]` 里跑的是 `--init-config --config "<安装目录>\config.toml"`。
        # 这里的 `--config` 是"写到哪儿"，而 `--config` 又恰好在 AGENT_FLAGS 里
        # （命令行 agent 用它读 agent.toml）——不先挡一下，整条会被当成命令行
        # agent 原样转交过去，那边 argparse 只回一句"不认识 --init-config"。
        return False
    if _flag_seen(argv, "--role"):
        return False
    if any(_flag_seen(argv, flag) for flag in HELP_FLAGS):
        return True
    return any(_flag_seen(argv, flag) for flag in AGENT_FLAGS)


@dataclass(frozen=True)
class GuiArgs:
    role: str | None = None
    config: str | None = None
    server: str | None = None
    token: str | None = None
    mode: str | None = None
    init_config: bool = False


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog=PROG,
        description="远程电脑助手。不带参数运行进入 PySide6 图形界面；"
                    "带 --server/--token 等参数时作为命令行客户端。",
        epilog="例：remote-pc.exe --role client --server ws://10.22.114.127:8765 "
               "--token <密钥>")
    ap.add_argument("--role", choices=ROLES,
                    help="直接进入 PySide6 工作台中的指定角色")
    ap.add_argument("--init-config", action="store_true",
                    help="生成一份新的 config.toml 后退出，不打开窗口。"
                         "安装包用它初始化配置；若配置文件已存在则不执行任何操作。")
    server = ap.add_argument_group("--role server")
    server.add_argument("--config", help="服务端 config.toml 的路径")
    client = ap.add_argument_group("--role client")
    client.add_argument("--server", help="服务端地址，如 ws://10.22.114.127:8765")
    client.add_argument("--token", help="预共享密钥")
    client.add_argument("--mode", choices=("safe", "confirm", "auto"),
                        help="客户端策略；未指定时沿用上次的选择")
    return ap


def parse_gui_args(argv: list[str]) -> GuiArgs:
    ap = build_parser()
    args = ap.parse_args(list(argv))
    if args.role == "server" and (args.server or args.token or args.mode):
        ap.error("服务器端不接受 --server/--token/--mode：密钥与端口均来自 config.toml。")
    if args.role == "client" and args.config:
        ap.error("客户端不读取服务端配置文件；--config 仅对 --role server 有效。")
    return GuiArgs(role=args.role, config=args.config, server=args.server,
                   token=args.token, mode=args.mode, init_config=args.init_config)


def set_console_title(title: str = CONSOLE_TITLE) -> bool:
    try:
        ctypes.windll.kernel32.SetConsoleTitleW(title)
        return True
    except Exception:  # noqa: BLE001 - 无控制台时忽略
        return False


# ===========================================================================
# 标准流
# ===========================================================================
# 黑框是必需品，不是装饰：它是对方随时能看见的命令记录，关掉黑框 = 断开，
# 而且它是这个项目里唯一"崩了也看得见"的地方。
#
# 但图形界面那一侧不要黑框——双击下去弹出一个黑框很丑。于是 exe 用
# `--windowed` 打，代价是**双击启动时 fd 1/2 是无效的，Python 会把
# sys.stdout / sys.stderr 设成 None**。实测（pythonw，无句柄启动）：
#
#   print("x")            没事   —— CPython 里 print 遇 None 会自己安静返回
#   sys.stderr.write("x") AttributeError: 'NoneType' object has no attribute 'write'
#
# 后者散在 agent、logging.StreamHandler()、uvicorn 里，绕不开。所以这里分两种处理：
#
#   走命令行 agent（`--server/--token/...`）→ **自己开一个黑框**。这条路要的就是黑框。
#   走图形界面                            → 指到 os.devnull：写进去的东西丢掉，但不炸。
#
# 已经有标准流的时候（从 cmd/powershell 里跑、或者被别的进程用管道接走）一概不动，
# 于是 `remote-pc.exe --help > out.txt` 照旧能拿到输出。

def _attach_console() -> bool:
    """给自己开一个控制台，把三个标准流接到它上面。

    只有 Windows 有这回事；也只有"双击起来 + 这一趟是命令行 agent"才走得到。
    开不出来返回 False，调用方退到 `os.devnull`（宁可看不见，也不能崩）。
    """
    if os.name != "nt":
        return False
    try:
        # 这里用的是模块顶上那个 ctypes —— 和 set_console_title 同一个。
        if not ctypes.windll.kernel32.AllocConsole():
            return False
        sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace",
                          buffering=1)
        sys.stderr = open("CONOUT$", "w", encoding="utf-8", errors="replace",
                          buffering=1)
        sys.stdin = open("CONIN$", "r", encoding="utf-8", errors="replace")
    except Exception:            # noqa: BLE001 - 开不出来不是致命事，退到 devnull
        return False
    return True


def _use_devnull() -> None:
    """把缺掉的标准流指到 devnull：写进去没了，但**不会炸**。"""
    null = open(os.devnull, "w", encoding="utf-8", errors="replace")
    if sys.stdout is None:
        sys.stdout = null
    if sys.stderr is None:
        sys.stderr = null


def _stream_plan(argv: list[str], missing: bool) -> str:
    """该拿标准流怎么办。纯函数，好测。

    missing = sys.stdout / sys.stderr 里有没有 None（也就是"背后有没有黑框"）。
    """
    if not missing:
        return "ok"                       # 有地方可写，一个字都不动
    return "console" if wants_agent_cli(argv) else "null"


def ensure_streams(argv: list[str], *, missing: bool | None = None,
                   attach=_attach_console, null=_use_devnull) -> str:
    """保证 `sys.stdout` / `sys.stderr` 能用。返回做了哪一种处理（也方便测）。

    `attach` / `null` 可注入：测试里不真去开控制台、不真动全局的 sys.stdout。
    """
    if missing is None:
        missing = sys.stdout is None or sys.stderr is None
    plan = _stream_plan(argv, missing)
    if plan == "console" and not attach():
        # 黑框没开出来。对方就看不见命令了，但**退出比瞎跑强**这种事不归这里管：
        # 继续跑，至少 hub 那边是通的，界面/日志里也还留得下话。
        plan = "null"
    if plan == "null":
        null()
        return "null"
    return plan


def run_agent_cli(argv: list[str]) -> int:
    from agent.agent import main as agent_main

    saved = sys.argv
    sys.argv = [saved[0] if saved else PROG, *argv]
    try:
        agent_main()
    finally:
        sys.argv = saved
    return 0


def _prefill(args: GuiArgs) -> Parts | None:
    """把命令行客户端参数解析成 PySide 页面可填入的字段。"""
    if not (args.server or args.token):
        return None
    if args.server and any(c.isspace() for c in args.server.strip()):
        print(f"无法解析命令行中的地址：{args.server!r}（地址中不应包含空格）", flush=True)
        return None
    parts = extract(f"--server {args.server or ''} --token {args.token or ''}")
    if parts.problem:
        print(f"无法解析命令行中的地址（{parts.problem}）", flush=True)
        return None
    if not parts.host:
        print(f"命令行中缺少服务器地址：{args.server!r}", flush=True)
        return None
    return parts


CRASH_FILE = "crash.log"


def _show_fatal(text: str, *, box=None) -> str:
    """界面起不来的时候，把话说到人看得见的地方，返回日志写到了哪。

    `--windowed` 打出来的 exe **没有黑框**，于是"启动就崩"的默认表现是
    ——双击下去，屏幕上一片安静，什么都没发生。这是这里唯一不能接受的结果：
    对方只会来问一句"你这东西是不是坏了"，而且没有任何线索可给。

    所以两件事一起做，顺序有讲究：

    1. **先落日志。** 用户看不看弹窗不重要，事后能翻才重要；弹窗被点掉就没了。
    2. **再弹窗。** 而且用 `user32.MessageBoxW` 而不是 Qt —— 崩在
       `import PySide6` 那一行时 Qt 根本还不在，用它弹窗等于没弹。
       位置也讲究：先尝试 Qt（界面已经起来的时候弹得好看些），失败再退到原生框。

    `box` 可注入（测试里不真弹窗）。
    """
    from pathlib import Path

    from . import paths

    where = ""
    try:
        log_dir = paths.log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        target = log_dir / CRASH_FILE
        with target.open("a", encoding="utf-8") as fh:
            fh.write(text.rstrip() + "\n")
        where = str(target)
    except Exception:            # noqa: BLE001 - 连日志都写不了也得往下走，弹窗是最后一道
        pass

    shown = text
    if where:
        shown += f"\n\n（详细信息已写入 {where}）"
    if box is None:
        box = _message_box
    try:
        box(shown)
    except Exception:            # noqa: BLE001
        pass
    return where


def _message_box(text: str) -> None:
    """尽力弹一个能看见的框。Qt 在就用 Qt，不在就用 user32。"""
    try:
        from PySide6 import QtWidgets

        QtWidgets.QMessageBox.critical(None, "远程电脑助手 启动失败", text)
        return
    except Exception:            # noqa: BLE001 - Qt 没起来（或压根没装），退到原生
        pass
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, text, "远程电脑助手 启动失败", 0x10)


def init_config(args: GuiArgs, *, out=None) -> int:
    """写一份全新的 config.toml 就退出。安装包用它播种配置。

    为什么让**程序自己**写、而不是在安装脚本里拼一份：模板只有一份
    （`gui/toml_edit.py::NEW_CONFIG_TEMPLATE`），安装脚本里再抄一份，
    两边早晚对不上——那正是这个项目在别处反复强调要避免的那种"看起来能用"。

    **已经有配置文件就什么都不做**，不覆盖、不备份、不"顺手换成新的"：
    用户机器上那份可能是他调过 `allowed_networks` 的。
    `out` 可注入（测试里不真打印）。
    """
    say = print if out is None else out

    from . import paths, toml_edit

    choice = paths.resolve_config_path(explicit=args.config)
    if choice.path.is_file():
        say(f"配置文件已存在，未作修改：{choice.path}")
        return 0

    from server.config import generate_token

    try:
        toml_edit.write_config(choice.path, toml_edit.render_new_config(generate_token()))
    except Exception as exc:      # noqa: BLE001 - 写不进去要说清楚写哪儿失败了
        say(f"无法写入配置文件（{choice.path}）：{exc}")
        return 1
    say(f"已生成配置文件：{choice.path}")
    return 0


def run_gui(args: GuiArgs) -> int:
    """启动唯一的 PySide6 图形界面。

    兜住异常：没有黑框之后，异常不接住就是"静默死亡"。
    `main()` 里那句 `print(BANNER)` 不需要同等待遇——CPython 的 print 遇到
    `sys.stdout is None` 会自己安静返回，不会炸。
    """
    import traceback

    try:
        from .pyside_app import run_pyside

        return run_pyside(args)
    except Exception as exc:     # noqa: BLE001
        _show_fatal(f"图形界面启动失败：{type(exc).__name__}: {exc}\n\n"
                    + traceback.format_exc())
        return 1


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    print(BANNER, flush=True)
    set_console_title()
    if wants_agent_cli(argv):
        if any(_flag_seen(argv, flag) for flag in HELP_FLAGS):
            print(HELP_NOTE, flush=True)
        return run_agent_cli(argv)
    args = parse_gui_args(argv)
    if args.init_config:
        # 不开窗口。安装包里跑的是这一支，所以它在 `--windowed` 下也得能写字——
        # `app.py::ensure_streams()` 已经把 stdout 补上了（管道 / devnull）。
        return init_config(args)
    return run_gui(args)


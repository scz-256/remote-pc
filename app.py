"""`python app.py` 和 PyInstaller 的入口。

这里**不放任何逻辑**：把标准流补齐、把输出改成 UTF-8、把 Windows 的事件循环
策略设好，剩下的全交给 `gui.app`。

`from gui.app import ...` 特意写在函数里而不是模块顶层，是为了让启动横幅
先打出去、图形界面之后才 import——单文件 exe 解包要 1~3 秒，
那段时间里屏幕上总得有点东西。
"""

from __future__ import annotations

import asyncio
import os
import sys


def main() -> int:
    from gui.app import ensure_streams, main as gui_main

    argv = sys.argv[1:]
    # 没有黑框时（`--windowed` 打出来的 exe、双击启动）先把标准流补齐：
    # 命令行 agent 那条路会顺手给自己开一个黑框，界面那条路丢进 devnull。
    # 为什么不在这儿实现：和 `set_console_title` 是同一件事，都住在 gui/app.py。
    ensure_streams(argv)
    # stdout 被重定向到文件/管道时，Windows 上的 Python 会退回本地代码页（cp936），
    # 中文日志就变成 GBK，跟项目里其他地方一律 UTF-8 对不上，事后翻日志很痛苦。
    # 真实控制台走的是 WriteConsoleW，不看这个编码，所以改了不会让窗口变乱码。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    # Windows 上要用 Proactor 才能跑 asyncio 子进程——客户端那条路要靠它。
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    return gui_main()


if __name__ == "__main__":
    sys.exit(main())

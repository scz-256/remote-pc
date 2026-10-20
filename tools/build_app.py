"""把**整个东西**打成一个 exe：同一个文件，两种角色。

    pip install pyinstaller
    python tools/build_app.py            # 默认：单文件、图形界面不带黑框
    python tools/build_app.py --console  # 带黑框的老样子，排查启动崩溃时用
    python tools/build_app.py --onedir   # 打成目录（安装包用这个）

产物：dist/remote-pc.exe（单文件）或 dist/remote-pc/remote-pc.exe（目录版）

双击 = 起 PySide6 工作台（不再弹角色选择黑框，直接进界面）。带
`--server/--token` 等参数跑 = 原来的命令行客户端，和 `remote-agent.exe`
行为一模一样——**包括它的黑框**：`app.py::ensure_streams()` 发现背后没有
控制台、而这一趟是命令行 agent 时会自己 `AllocConsole()` 开一个。

刻意的选择，一条都别随手改：

  * **--windowed。** 图形界面不该配一个黑框。代价是"启动就崩"在屏幕上
    **什么都不显示**，所以另外补了三层，缺一不可：
      1. `app.py::ensure_streams()` —— 没有控制台时把 stdout/stderr 补齐
         （`None` 上 `.write()` 会 AttributeError，而这东西散在 logging、
         uvicorn、agent 里，绕不开）；
      2. `gui/app.py::run_gui()` —— 兜住异常，不再"静默死亡"；
      3. `gui/app.py::_show_fatal()` —— 先写 `logs/crash.log`，再用
         user32 原生弹窗告诉用户（**不用 Qt**：崩在 import PySide6 那一步时
         Qt 根本还没起来）。
    想临时换成带黑框的（比如要盯启动阶段的每一步），`--console`。
  * **不加壳、不压缩（--noupx）。** 对方拿到的东西应该是能
    `pyinstaller --extract` 拆开看的，不是一团黑盒。
  * **不排除 fastapi / uvicorn / httpx / websockets / PySide6**——服务端角色
    要在同一个进程里跑 hub，客户端要跑 PySide6 窗口，这些全是必需品。
  * **但 PySide6 只收 `import` 得到的那几个模块**，不整包收。
    PyInstaller 的 `--collect-data PySide6` 会把 site-packages 里那 634 MB
    全铺出来，其中 `Qt6WebEngineCore.dll`（Chromium 内核，195 MB）、
    `resources/` 下的 `.pak`（102 MB，也是 WebEngine 的）、`translations`（60 MB）、
    `qml`（30 MB）、`opengl32sw.dll`（20 MB）跟这个界面**一点关系都没有**——
    工作台只 `from PySide6 import QtCore, QtGui, QtWidgets`。
    PyInstaller 6 的 `hook-PySide6.Qt*` 是**按模块**收集的，所以放开让钩子
    自己判断就够了：Qt6Core/Gui/Widgets 那几个 dll、`platforms/qwindows.dll`、
    `styles`、`imageformats` 该在的都会在，多余的不会来。
    **别再把 `--collect-binaries/--collect-data PySide6` 加回来。**
  * **EXCLUDES 里那四个包（numpy / PIL / lxml / Cython，约 49 MB）也是同理。**
    我们自己的代码一行都不 import 它们，PyInstaller 是顺着某条可选依赖静态收进来的。
    判据是实测的：把 fastapi、uvicorn、httpx、websockets、pydantic、
    server.hub、server.mcp_server、gui.app、gui.pyside_app、agent.agent 全 import
    一遍，**一个都没被碰**（只有 Cython 被 `pydantic.v1.version` 摸了一下，
    而那一句本身就在 `try/except ImportError` 里）。剔掉之后那边 `import` 抛的是
    ModuleNotFoundError，和"这台机器上没装"完全一样。
  * **排除 mcp。** 图形界面走 `RemoteClient`，`server/mcp_server.py` 一行都不 import；
    它拖着整个 MCP SDK，白白几 MB。
  * **显式 --hidden-import server.hub。** `gui/hubctl.py` 里是用
    `importlib.import_module("server.hub")` 懒加载的（配置写坏了也不能让
    客户端起不来），**动态字符串 import 静态分析跟不到**，不写死就会打出
    一个"选服务器端点下去就报 ModuleNotFoundError"的 exe。
  * **--onefile 还是 --onedir。** 默认单文件：拷到哪台机器上都能跑，
    双击就走，不用带一个文件夹。代价是**每次启动都要把 ~70 MB 解到
    `%TEMP%`**（实测 1~3 秒，慢盘更久），而 %TEMP% 里那堆东西程序退出就删。

    装安装包的时候用 `--onedir`：几百个文件铺在安装目录里，启动几乎瞬时，
    而"一堆文件"这个复杂度正好是安装包最擅长的部分——它本来就是来铺文件的。
    谁走哪条路：`tools/build_installer.py` 用 `--onedir`，
    "拷个 exe 过去"那种分发继续用默认的单文件。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENTRY = ROOT / "app.py"
DIST = ROOT / "dist"
BUILD = ROOT / "build"
NAME = "remote-pc"
WORK = BUILD / NAME
# exe 的图标。没有就不传 --icon，PyInstaller 用默认图标——打包本身不受影响。
ICON = ROOT / "assets" / "remote-pc.ico"

# 静态分析看不见的那些：hubctl 用字符串 import 它们（懒加载是故意的）。
HIDDEN = ["PySide6", "server.hub", "server.config", "server.audit", "server.policy",
          "server.protocol", "server.registry", "server.remote", "uvicorn"]

# 界面这条路一个都不碰的东西。
#
# numpy / PIL / lxml / Cython 这四个不是我们写的，也不在我们写的任何一条路上：
# 运行时把 fastapi、uvicorn、httpx、websockets、pydantic、server.hub、
# server.mcp_server、gui.app、gui.pyside_app、agent.agent 全 import 一遍，
# **一个都没被碰**（只有 Cython 被 pydantic.v1.version 摸了一下，而那是
# `try: import cython / except ImportError:` 包着的，本来就允许没有）。
# PyInstaller 是静态分析顺着某条可选依赖把它们收进来的，一共 49 MB。
# 剔掉之后那边 `import` 会抛 ModuleNotFoundError——和"这台机器上没装"完全一样，
# 外面那层 try/except 照样接得住。
EXCLUDES = ["mcp", "uvloop", "watchfiles", "dotenv", "yaml",
            "numpy", "PIL", "lxml", "Cython"]


def _utf8_stdout() -> None:
    """让自己打的中文（还有 ✓ ✗）到哪都写得出去。

    控制台里本来就没事（Python 走 WriteConsoleW，UTF-8 照样显示），但**输出一被
    重定向**就换成按本地代码页编码（这里是 GBK），`✓` 直接
    `UnicodeEncodeError` —— 于是"打完包把日志存下来"这个再正常不过的动作，
    会让脚本在打印"成功"的那一行炸掉。和 `app.py` 里那两行同一个理由。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    _utf8_stdout()
    ap = argparse.ArgumentParser(description="打包 remote-pc.exe")
    ap.add_argument("--console", action="store_true",
                    help="带黑框打包（老样子）。默认 --windowed，图形界面不要黑框。")
    ap.add_argument("--onedir", action="store_true",
                    help="打成目录而不是单文件。启动快得多（不用每次解 70 MB 到 "
                         "%%TEMP%%），但分发的是一个文件夹。安装包用这个。")
    args = ap.parse_args(list(sys.argv[1:] if argv is None else argv))

    if not ENTRY.is_file():
        print(f"找不到入口：{ENTRY}")
        return 2

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("没装 PyInstaller。先跑：pip install pyinstaller")
        return 2

    # 只清理自己的工作目录，保留 dist/remote-agent.exe，方便两个产物并存。
    if WORK.exists():
        shutil.rmtree(WORK, ignore_errors=True)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onedir" if args.onedir else "--onefile",
        "--console" if args.console else "--windowed",   # 见文件头那三条补救
        "--noupx",                # 不加壳
        "--clean",
        "--noconfirm",
        "--name", NAME,
        "--distpath", str(DIST),
        "--workpath", str(WORK),
        "--specpath", str(BUILD),
        "--paths", str(ROOT),
    ]
    if ICON.is_file():
        # --icon 只管 exe 文件本身在资源管理器里长什么样；窗口/任务栏图标是
        # 程序运行时从 assets\ 读的（gui/paths.py::asset_path），所以图标文件
        # 也得跟着打包进去。
        cmd += ["--icon", str(ICON)]
        cmd += ["--add-data", f"{ICON}{os.pathsep}assets"]
    for name in HIDDEN:
        cmd += ["--hidden-import", name]
    cmd += ["--collect-binaries", "shiboken6",
            "--collect-data", "shiboken6"]
    # Qt6Core 依赖 ICU。项目环境里还可能有其它包提供同名 ICU DLL，
    # 明确把 PySide6 自己的版本放到 PySide6 子目录，避免 onefile 加载错版本。
    pyside_dir = Path(sys.executable).parent / "Lib" / "site-packages" / "PySide6"
    for dll in sorted(pyside_dir.glob("icu*.dll")):
        cmd += ["--add-binary", f"{dll};PySide6"]
    for name in EXCLUDES:
        cmd += ["--exclude-module", name]
    cmd += ["--exclude-module", "tkinter", "--exclude-module", "_tkinter"]
    cmd.append(str(ENTRY))

    print("$ " + " ".join(cmd))
    # Qt6Core 使用 Windows 系统 ICU（未带版本后缀的导出符号）。当前开发环境
    # 还通过 Codex 的 Poppler 路径提供了一套 ICU 78；如果让 PyInstaller 看见
    # 那条 PATH，它会把不兼容的 icuuc.dll 收进包，运行时覆盖系统 ICU，导致
    # ``DLL load failed ... 找不到指定的程序``。构建时过滤这条仅用于工具链的路径，
    # 让 Qt 依赖保持系统 DLL 解析。
    build_env = os.environ.copy()
    build_env["PATH"] = os.pathsep.join(
        part for part in build_env.get("PATH", "").split(os.pathsep)
        if "poppler" not in part.lower()
    )
    rc = subprocess.call(cmd, env=build_env)
    if rc != 0:
        return rc

    exe = (DIST / NAME / f"{NAME}.exe") if args.onedir else (DIST / f"{NAME}.exe")
    if not exe.is_file():
        print("构建结束但没看到 exe，检查上面的输出。")
        return 1

    print()
    if args.onedir:
        total = sum(f.stat().st_size for f in exe.parent.rglob("*") if f.is_file())
        print(f"好了：{exe}  (整个目录 {total / 1024 / 1024:.1f} MB)")
    else:
        print(f"好了：{exe}  ({exe.stat().st_size / 1024 / 1024:.1f} MB)")
    print()
    if not _smoke(exe):
        return 1

    print()
    if args.onedir:
        print("这一版是**目录**，分发时整个文件夹一起走（别只拷 exe）：")
        print(f"  装安装包：python tools/build_installer.py")
    else:
        print("给它拷到哪台机器上都能跑（目标机器不需要装 Python）：")
    print(f"  {NAME}.exe                      → 起 PySide6 工作台（不带黑框）")
    print(f"  {NAME}.exe --role server         → 直接当服务器端")
    print(f"  {NAME}.exe --init-config         → 只写一份 config.toml 就退出")
    print(f'  {NAME}.exe --server ws://… --token … --mode confirm')
    print("                                   → 原来的命令行客户端")
    print()
    print("图形窗口那一步（PySide6 有没有真的打进去）要人工看一眼：")
    print(f"  双击 {exe}（不该出现黑框），概览页上点「进入工作台」再切到"
          "服务器端，窗口里应该显示出本机 IPv4。")
    return 0


def _smoke(exe: Path) -> bool:
    """打完先自己跑三条不会开窗口的命令。

    1. `--help` 证明命令行那条路还在。窗口版也要拿到输出：这里给了管道，
       于是 exe 里的 `sys.stdout` 是好的，`ensure_streams()` 一个字都不动。
    2. `--role boss` 证明图形界面那套参数解析也在、而且**参数错的时候不弹
       窗口**（这一条正是界面的立场），退出码是 2 而不是 1。
    3. **没有任何可继承句柄地启动**（= 双击的真实情况，也是 `--windowed` 下
       唯一会踩到 `sys.stdout is None` 的场景）。这条钉的是
       `app.py::ensure_streams()`：少了它，`sys.stderr.write()` 会
       AttributeError，进程还没走到 argparse 就死了。

    `encoding="utf-8"` 是必须的，不是顺手写的：exe 里的 `app.py` 会把
    stdout/stderr 改成 UTF-8（真实控制台走 WriteConsoleW 不受影响），
    这边要是按本地代码页解，read 线程会死在 `UnicodeDecodeError` 上，
    拿到的 `stdout` 是 `None`——看着像"exe 没输出"，其实是读的人解错了码。
    """
    ok = True

    got = subprocess.run([str(exe), "--help"], capture_output=True,
                         encoding="utf-8", errors="replace", timeout=180)
    if got.returncode == 0 and "远程终端 agent" in (got.stdout or ""):
        print("  ✓ --help 通（命令行那条路还在，窗口版照样能往管道里写）")
    else:
        ok = False
        print(f"  ✗ --help 不对：rc={got.returncode}\n{got.stdout}\n{got.stderr}")

    got = subprocess.run([str(exe), "--role", "boss"], capture_output=True,
                         encoding="utf-8", errors="replace", timeout=180)
    if got.returncode == 2 and "invalid choice" in (got.stderr or ""):
        print("  ✓ --role 参数错 → 退出码 2，没弹窗口")
    else:
        ok = False
        print(f"  ✗ --role 那一步不对：rc={got.returncode}\n{got.stdout}\n{got.stderr}")

    if os.name == "nt":
        # 无句柄 + 脱离控制台：等价于在资源管理器里双击。`close_fds=True`
        # 且不传 stdin/stdout/stderr，子进程的 0/1/2 是无效的。
        got = subprocess.run([str(exe), "--role", "boss"], close_fds=True,
                             creationflags=0x00000008,  # DETACHED_PROCESS
                             timeout=180)
        if got.returncode == 2:
            print("  ✓ 无句柄启动（双击那种）没崩：rc=2")
        else:
            ok = False
            print(f"  ✗ 无句柄启动不对：rc={got.returncode}（期望 2）")
    return ok


if __name__ == "__main__":
    sys.exit(main())

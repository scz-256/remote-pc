"""把 agent 打成一个单文件 exe，方便丢给同学的电脑跑。

    pip install pyinstaller
    python tools/build_agent.py

产物：dist/remote-agent.exe

刻意的选择：
  * --console（**不**用 --windowed）。黑框必须留在屏幕上，
    对方随时看得见每条命令、随时能关掉窗口断开——这是这个工具的性质，
    不是缺陷。打包成"无窗口后台程序"就不该用这个脚本。
  * 单文件、不加壳、不压缩。对方拿到的东西应该是能用
    pyinstaller --extract 或直接看源码对照的，不是一团黑盒。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENT = ROOT / "agent" / "agent.py"
DIST = ROOT / "dist"
BUILD = ROOT / "build"
NAME = "remote-agent"
WORK = BUILD / NAME
# 单独给 agent 画了图标就用它，没有就先和主程序用同一个。
ICON = ROOT / "assets" / "remote-agent.ico"
if not ICON.is_file():
    ICON = ROOT / "assets" / "remote-pc.ico"


def main() -> int:
    if not AGENT.is_file():
        print(f"找不到 agent 源码：{AGENT}")
        return 2

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("没装 PyInstaller。先跑：pip install pyinstaller")
        return 2

    # 只清理自己的工作目录，保留 dist/remote-pc.exe，方便两个产物并存。
    if WORK.exists():
        shutil.rmtree(WORK, ignore_errors=True)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--console",              # 见文件头：黑框必须留着
        "--clean",
        "--noconfirm",
        "--name", NAME,
        "--distpath", str(DIST),
        "--workpath", str(WORK),
        "--specpath", str(BUILD),
        # agent 是单文件、零第三方依赖的，不需要把 server/ 或 mcp 一起塞进去
        "--exclude-module", "fastapi",
        "--exclude-module", "uvicorn",
        "--exclude-module", "httpx",
        "--exclude-module", "mcp",
    ]
    if ICON.is_file():
        cmd += ["--icon", str(ICON)]
    cmd.append(str(AGENT))
    print("$ " + " ".join(cmd))
    rc = subprocess.call(cmd)
    if rc != 0:
        return rc

    exe = DIST / f"{NAME}.exe"
    if not exe.is_file():
        print("构建结束但没看到 exe，检查上面的输出。")
        return 1

    print()
    print(f"好了：{exe}  ({exe.stat().st_size / 1024 / 1024:.1f} MB)")
    print()
    print("把它拷给对方，让对方双击、或者在命令行里跑：")
    print(f'  {NAME}.exe --server ws://<你的局域网IP>:8765 --token <密钥> --mode confirm')
    print()
    print("对方的窗口会一直显示收到的每条命令。关掉窗口 = 立刻断开。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

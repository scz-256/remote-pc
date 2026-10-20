"""打一个安装包：`远程电脑助手-安装程序.exe`。

    pip install pyinstaller
    winget install JRSoftware.InnoSetup     # 只要一次（见下面"为什么是 Inno"）
    python tools/build_installer.py

产物：dist/远程电脑助手-安装程序.exe

它做三件事，顺序不能换：

  1. **先要一份目录版**（`dist/remote-pc/`）。`build_app.py --onedir` 打出来，
     已经在了就跳过——163 MB 的包重打一次要一分多钟。
  2. **探 Inno Setup 在不在**。不在就明说怎么装，然后**退回 0**：
     目录版本身已经是个能用的产物，不该因为缺一个可选工具就整体失败。
  3. **调 iscc** 编译 `tools/installer/remote-pc.iss`。

为什么是 Inno Setup：

  * 脚本是**纯文本的 .iss**，能进 git、能 diff——这个项目里别的东西都是这个标准。
  * 默认按当前用户安装（不需要管理员权限），而且**装哪儿由用户选**：
     安装向导里有选目录那一页，装 D 盘、装 U 盘都行。
     默认落在 `%LOCALAPPDATA%\\Programs\\远程电脑助手`，那个目录可写——
     界面上的「随机生成密钥」要能把 config.toml 写回去。
  * 卸载、开始菜单、桌面快捷方式、写注册表项，都是声明式的几行。

**agent 那一头不做安装包**（`tools/build_agent.py` 保持单文件）：它的卖点是
"一个文件丢过去双击就跑"，加一步安装等于把这个优点换掉。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ISS = ROOT / "tools" / "installer" / "remote-pc.iss"
DIST = ROOT / "dist"
APP_DIR = DIST / "remote-pc"            # --onedir 的产物目录
APP_EXE = APP_DIR / "remote-pc.exe"
BUILD_APP = ROOT / "tools" / "build_app.py"
# 安装程序 exe 的图标。.iss 里有相对路径的退路，这里给绝对路径免得依赖 cwd。
ICON = ROOT / "assets" / "remote-pc.ico"

# Inno 的编译器可能在这几个地方（winget 装的是第一个）。
ISCC_HINTS = (
    r"%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe",
    r"%ProgramFiles%\Inno Setup 6\ISCC.exe",
    r"%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe",
    r"%ProgramFiles%\Inno Setup 7\ISCC.exe",
)

# 中文界面不是 Inno 自带语言包。有就用，没有就英文——不因此失败。
CHINESE_ISL = ("Inno Setup 6", "Inno Setup 7")


def _utf8_stdout() -> None:
    """和 build_app.py 同一个理由：输出一被重定向，Windows 就按本地代码页
    （GBK）编码，中文和 ✓ 直接 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def find_iscc() -> Path | None:
    """找到 Inno 的编译器。PATH 优先，然后几个常见安装位置。"""
    on_path = shutil.which("iscc") or shutil.which("ISCC")
    if on_path:
        return Path(on_path)
    for hint in ISCC_HINTS:
        expanded = Path(os.path.expandvars(hint))
        if expanded.is_file():
            return expanded
    return None


def find_chinese_isl(iscc: Path) -> Path | None:
    """中文语言包在 iscc 旁边的 Languages\\ 里（有的话）。"""
    for candidate in (iscc.parent / "Languages" / "ChineseSimplified.isl",
                      iscc.parent.parent / "Languages" / "ChineseSimplified.isl"):
        if candidate.is_file():
            return candidate
    return None


def app_version() -> str:
    """版本号只有 `gui/app.py::APP_VERSION` 一份，不在这儿另写一个。"""
    sys.path.insert(0, str(ROOT))
    from gui.app import APP_VERSION

    return APP_VERSION


def ensure_onedir(*, force: bool = False) -> bool:
    """目录版没有（或者要求重打）就跑一遍 `build_app.py --onedir`。"""
    if APP_EXE.is_file() and not force:
        size = sum(f.stat().st_size for f in APP_DIR.rglob("*") if f.is_file())
        print(f"目录版已经在：{APP_EXE}（{size / 1024 / 1024:.1f} MB）—— 不重打。")
        print("  想重打：python tools/build_app.py --onedir")
        return True

    print("先打目录版（--onedir，启动比单文件快得多）…")
    rc = subprocess.call([sys.executable, str(BUILD_APP), "--onedir"])
    if rc != 0 or not APP_EXE.is_file():
        print(f"目录版没打出来（退出码 {rc}），安装包就没得装了。")
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    _utf8_stdout()
    argv = list(sys.argv[1:] if argv is None else argv)
    force = "--force-build" in argv

    if not ISS.is_file():
        print(f"找不到安装脚本：{ISS}")
        return 2

    if not ensure_onedir(force=force):
        return 1

    iscc = find_iscc()
    if iscc is None:
        print()
        print("没找到 Inno Setup 的编译器（ISCC.exe），装一个再跑：")
        print("    winget install JRSoftware.InnoSetup")
        print("  或去 https://jrsoftware.org/isdl.php 下 6.3 以上版本。")
        print()
        print(f"不装也不影响别的：目录版已经好了 → {APP_EXE}")
        print("  （把整个 dist\\remote-pc\\ 文件夹拷过去，双击里面的 exe 就能用。）")
        return 0

    version = app_version()
    cmd = [
        str(iscc),
        f"/DAppVersion={version}",
        f"/DSourceDir={APP_DIR}",
        f"/DOutputDir={DIST}",
        "/Qp",
        str(ISS),
    ]
    if ICON.is_file():
        # 用正斜杠：反斜杠在 ISPP 的宏里会被当转义符。
        cmd.insert(-1, f"/DSetupIcon={ICON.as_posix()}")
    else:
        print(f"（没找到 {ICON}，安装程序会用 Inno 的默认图标。）")

    chinese = find_chinese_isl(iscc)
    if chinese is not None:
        # 用正斜杠：反斜杠在 ISPP 的宏里会被当转义符。
        cmd.insert(-1, f"/DChineseIsl={chinese.as_posix()}")
    else:
        print("（没找到中文语言包 ChineseSimplified.isl，安装界面走英文。）")

    print(f"$ {' '.join(cmd)}")
    rc = subprocess.call(cmd)
    if rc != 0:
        print(f"iscc 编译失败，退出码 {rc}")
        return rc

    out = DIST / f"{out_name(ISS)}.exe"
    print()
    if out.is_file():
        print(f"好了：{out}  ({out.stat().st_size / 1024 / 1024:.1f} MB)")
        print("  把它发给对方，装完就有开始菜单/桌面快捷方式，")
        print("  配置固定落在安装目录的 config.toml，不用再去猜它在哪。")
    else:
        print(f"iscc 说成功了，但没看到 {out} —— 去 dist\\ 里翻一下。")
        return 1
    return 0


def out_name(iss: Path) -> str:
    """从 .iss 里读 OutputBaseFilename，省得在这儿再抄一遍文件名。"""
    for line in iss.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("outputbasefilename="):
            return stripped.split("=", 1)[1].strip()
    return "远程电脑助手-安装程序"


if __name__ == "__main__":
    sys.exit(main())

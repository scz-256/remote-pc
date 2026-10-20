"""配置文件、日志目录到底放哪。

两个坑，都是打包成 exe 之后才踩得到的：

1. **不能从 `__file__` 推路径。** 单文件模式里它指向 PyInstaller 解压出来的
   `%TEMP%\\_MEIxxxxxx`，那个目录**退出时就删掉**。`server/config.py` 的
   `PROJECT_ROOT` 就是这么算的，所以冻结时它是个假路径。

2. **不能凭空另起一份配置。** 操作者机器上已经有一份能用的 `config.toml`，
   MCP 和 CLI 都读它。GUI 要是自己生成一把新密钥写别处，那整套配置会**静默失效**——
   现象是"CC 忽然说 token 无效"，很难往回追。所以查找顺序里特意有
   "从 exe 目录往上找几层"：`dist\\remote-pc.exe` 会找到仓库根那份 `config.toml`，
   也就是原来那一份。界面上再把"用了哪个文件、怎么找到的"显示出来，就没有魔法了。
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

CONFIG_NAME = "config.toml"
SERVER_CONFIG_NAME = "server.toml"
APP_DIR_NAME = "remote-pc"
ASSETS_DIR_NAME = "assets"

# 从程序所在目录往上找几层。3 层够覆盖 dist/ 和 build/xxx/ 这种常见安放方式，
# 又不会一路爬到盘根去撞上一个不相干的 config.toml。
DEFAULT_MAX_UP = 3


@dataclass(frozen=True)
class ConfigChoice:
    path: Path
    source: str            # 人话说明这个路径是怎么来的，直接显示在界面上
    exists: bool


def _env(environ: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if environ is None else environ


def _is_frozen(frozen: bool | None) -> bool:
    return bool(getattr(sys, "frozen", False)) if frozen is None else frozen


def app_dir(*, frozen: bool | None = None, exe_dir: str | Path | None = None) -> Path:
    """程序自己所在的目录。

    冻结时 = exe 所在目录；跑源码时 = 仓库根（这里用 `__file__` 是安全的，
    因为没冻结，它就是个真实存在的路径）。
    """
    if exe_dir is not None:
        return Path(exe_dir)
    if _is_frozen(frozen):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def asset_path(name: str, *, frozen: bool | None = None,
               bundle: str | Path | None = None) -> Path:
    """随程序一起打包的静态资源（目前只有图标）在哪。

    冻结时在 PyInstaller 的解包目录里（`sys._MEIPASS`，目录版是 exe 旁边的
    `_internal`）——单文件模式那个目录退出即删，所以**只能读，不能往里写**；
    跑源码时是仓库根下的 `assets/`。

    打不进去时返回一个不存在的路径，调用方用 `is_file()` 判断即可，
    不在这儿抛异常：图标丢了不该让界面起不来。
    """
    if _is_frozen(frozen):
        root = Path(bundle if bundle is not None else getattr(sys, "_MEIPASS", ""))
        return root / ASSETS_DIR_NAME / name
    return Path(__file__).resolve().parent.parent / ASSETS_DIR_NAME / name


def data_dir(*, environ: Mapping[str, str] | None = None,
             home: Path | None = None) -> Path:
    """放"跟着人走、不跟着程序走"的东西：记住的连接信息、日志。

    默认 `%LOCALAPPDATA%\\remote-pc`。同学的 exe 可能放在 U 盘上，
    而这些文件里有密钥，所以不能放程序旁边。
    """
    env = _env(environ)
    override = env.get("REMOTE_PC_DATA", "").strip()
    if override:
        return Path(override).expanduser()
    base = env.get("LOCALAPPDATA", "").strip()
    if base:
        return Path(base) / APP_DIR_NAME
    return (Path(home) if home is not None else Path.home()) / APP_DIR_NAME


def ensure_writable(directory: Path) -> bool:
    """这个目录能不能写。

    不能写就退到用户目录——"保存密钥"按钮点了没反应、
    或者更糟：写到一半失败留下个半截文件，都是不能接受的。
    """
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    return os.access(directory, os.W_OK)


def resolve_config_path(
    *,
    explicit: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
    exe_dir: str | Path | None = None,
    frozen: bool | None = None,
    data: str | Path | None = None,
    max_up: int = DEFAULT_MAX_UP,
) -> ConfigChoice:
    """服务端的 config.toml 用哪一个。先中先得。"""
    env = _env(environ)
    here = app_dir(frozen=frozen, exe_dir=exe_dir)

    if explicit:
        path = Path(str(explicit)).expanduser()
        return ConfigChoice(path, "命令行 --config", path.is_file())

    env_path = env.get("REMOTE_PC_CONFIG", "").strip()
    if env_path:
        path = Path(env_path).expanduser()
        return ConfigChoice(path, "环境变量 REMOTE_PC_CONFIG", path.is_file())

    beside = here / CONFIG_NAME
    if beside.is_file():
        return ConfigChoice(beside, "程序所在目录", True)

    parent = here
    for level in range(1, max_up + 1):
        nxt = parent.parent
        if nxt == parent:
            break                      # 已经到盘根了，再往上没有意义
        parent = nxt
        candidate = parent / CONFIG_NAME
        if candidate.is_file():
            return ConfigChoice(candidate, f"上层目录（{level} 级）", True)

    user_config = (Path(data) if data is not None else data_dir(environ=env)) / SERVER_CONFIG_NAME
    if user_config.is_file():
        return ConfigChoice(user_config, "用户目录", True)

    # 一处都没找到：这一份要新建。写哪儿？
    # 优先写程序旁边（看得见、好交付、好备份）；那儿不可写才写用户目录。
    if ensure_writable(here):
        return ConfigChoice(beside, "新建（程序所在目录）", False)
    return ConfigChoice(user_config, "新建（用户目录）", False)


def log_dir(
    *,
    exe_dir: str | Path | None = None,
    frozen: bool | None = None,
    data: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """hub 的日志 / 审计日志放哪。

    思路和 `agent.default_audit_path()` 一致：程序旁边优先（看得见），
    不可写退到用户目录。**冻结时绝不从 `__file__` 推** —— 那是 `%TEMP%\\_MEIxxxxxx`，
    退出即删，审计日志放那儿等于没留。
    """
    env = _env(environ)
    here = app_dir(frozen=frozen, exe_dir=exe_dir)
    if ensure_writable(here):
        return here / "logs"
    return (Path(data) if data is not None else data_dir(environ=env)) / "logs"

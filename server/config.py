"""服务端配置。优先级：环境变量 > config.toml > 默认值。"""

from __future__ import annotations

import os
import secrets
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import policy

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.toml"
LOG_DIR = PROJECT_ROOT / "logs"

# 只允许这些网段连进来。默认都是私有地址——公网 IP 会被拒。
DEFAULT_ALLOWED_NETWORKS = [
    "127.0.0.0/8",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
]


@dataclass
class ServerConfig:
    token: str = ""
    host: str = "0.0.0.0"
    port: int = 8765
    allowed_networks: list[str] = field(
        default_factory=lambda: list(DEFAULT_ALLOWED_NETWORKS)
    )
    mode: str = policy.MODE_CONFIRM
    require_agent: bool = True          # 没连上 agent 时，API 直接报错而不是干等
    exec_default_timeout_s: float = 120.0
    single_agent_id: bool = True       # 同一 agent_id 只允许一条连接，新的顶掉旧的
    hub_url: str = "http://127.0.0.1:8765"   # CLI / MCP 从这里找 hub

    @property
    def ws_url(self) -> str:
        return f"ws://{self.host}:{self.port}"


def _load_toml(path: Path) -> dict:
    if not path.is_file():
        return {}
    with path.open("rb") as fh:
        return tomllib.load(fh)


def load_config(path: Path | str | None = None) -> ServerConfig:
    # 显式路径 > %REMOTE_PC_CONFIG% > 仓库里的 config.toml。
    # 中间这一层是给图形界面和冻结版准备的：改一个环境变量就能让
    # `python -m server.hub` / CLI / MCP 全都改读另一个文件，不用动源码。
    if path:
        cfg_path = Path(path)
    else:
        override = os.environ.get("REMOTE_PC_CONFIG", "").strip()
        cfg_path = Path(override).expanduser() if override else DEFAULT_CONFIG_PATH
    raw = _load_toml(cfg_path)
    server = raw.get("server", {})

    cfg = ServerConfig(
        token=server.get("token", ""),
        host=server.get("host", "0.0.0.0"),
        port=int(server.get("port", 8765)),
        allowed_networks=list(server.get("allowed_networks", DEFAULT_ALLOWED_NETWORKS)),
        mode=server.get("mode", policy.MODE_CONFIRM),
        require_agent=bool(server.get("require_agent", True)),
        exec_default_timeout_s=float(server.get("exec_default_timeout_s", 120.0)),
        single_agent_id=bool(server.get("single_agent_id", True)),
        hub_url=server.get("hub_url", f"http://127.0.0.1:{int(server.get('port', 8765))}"),
    )

    # 环境变量覆盖，方便临时换 token 而不改文件
    cfg.token = os.environ.get("REMOTE_PC_TOKEN", cfg.token)
    cfg.port = int(os.environ.get("REMOTE_PC_PORT", cfg.port))
    cfg.hub_url = os.environ.get("REMOTE_PC_HUB_URL", cfg.hub_url)
    if os.environ.get("REMOTE_PC_MODE"):
        cfg.mode = os.environ["REMOTE_PC_MODE"]

    if cfg.mode not in policy.MODES:
        raise ValueError(f"config 里的 mode={cfg.mode!r} 无效，应为 {policy.MODES}")

    return cfg


def generate_token(nbytes: int = 32) -> str:
    """生成预共享密钥。用 url-safe base64，方便手抄和放进 TOML。"""
    return secrets.token_urlsafe(nbytes)

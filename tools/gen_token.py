"""生成预共享密钥，写进服务端 config.toml，并打印对方要用的命令。

    python tools/gen_token.py            # 已有 token 就保留，只是看看
    python tools/gen_token.py --force    # 强制换一把新的

换 token 后两边都要重启：hub、agent。

配置模板和局域网 IP 探测都从 gui/ 里拿，保证只有一份实现
（图形界面的服务器端窗口用的是同一套，两边不会给出不一样的地址）。
"""

from __future__ import annotations

import argparse
import re
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gui.netinfo import list_lan_ipv4                  # noqa: E402
from gui.toml_edit import save_token_to_config         # noqa: E402

CONFIG = ROOT / "config.toml"


def current_token() -> str | None:
    if not CONFIG.is_file():
        return None
    m = re.search(r'^\s*token\s*=\s*"([^"]+)"', CONFIG.read_text(encoding="utf-8"), re.M)
    return m.group(1) if m else None


def main() -> int:
    ap = argparse.ArgumentParser(description="生成预共享密钥")
    ap.add_argument("--force", action="store_true", help="即使已有 token 也换新的")
    args = ap.parse_args()

    existing = current_token()
    if existing and not args.force:
        token = existing
        print(f"config.toml 里已有 token，保留不变：{token[:8]}…")
    else:
        token = secrets.token_urlsafe(32)
        # 走 gui/toml_edit 而不是自己 write_text 一份模板：模板会把操作者自己写的
        # 注释、改过的 allowed_networks 和 exec_default_timeout_s 全部冲掉。
        # 换一把密钥不该顺手改掉人家的配置。文件不存在时才用模板新建。
        result = save_token_to_config(CONFIG, token)
        if not result.ok:
            print(f"写不进 {CONFIG}：{result.text}", file=sys.stderr)
            return 1
        print(result.text)

    ips = list_lan_ipv4()
    print("\ntoken:")
    print(f"  {token}")
    print("\n让同学在他的电脑上跑（把 IP 换成你这边局域网地址）：")
    if ips:
        for ip in ips:
            print(f"  python agent.py --server ws://{ip}:8765 --token {token}")
    else:
        print(f"  python agent.py --server ws://<你的局域网IP>:8765 --token {token}")
        print("  （没自动探测到局域网 IP，用 ipconfig 看一下）")
    print("\n我这边启动 hub：")
    print("  python -m server.hub")
    print("\n注意：hub 监听 8765，第一次运行 Windows 可能会弹防火墙提示，选「专用网络」允许。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

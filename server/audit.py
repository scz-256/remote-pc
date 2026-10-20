"""审计日志：每一条远程指令都留痕，事后任一方都能完整对账。

写 JSONL 而不是纯文本，是为了能直接喂给脚本做统计。
agent 侧有一份等价实现（agent 要保持单文件）。
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import LOG_DIR


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


class AuditLog:
    """追加写 JSONL。用锁保证多协程/线程下不会写出半行。"""

    def __init__(self, path: Path | None = None) -> None:
        self._lock = threading.Lock()
        if path is None:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
            path = LOG_DIR / f"audit-{stamp}.jsonl"
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: str, **fields) -> None:
        record = {"ts": time.time(), "event": event}
        record.update(fields)
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path


def atomic_write(path: Path, content: bytes) -> None:
    fd, name = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        for attempt in range(6):
            try:
                os.replace(temporary, path)
                break
            except PermissionError as exc:
                # Windows 讀取中的檔案可能短暫阻擋原子替換。
                if getattr(exc, "winerror", None) not in {5, 32} or attempt == 5:
                    raise
                time.sleep(.02)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def write_json(path: Path, value: dict) -> None:
    atomic_write(path, json.dumps(value, ensure_ascii=False).encode("utf-8"))

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

_lock = threading.RLock()


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def append_jsonl(path: Path, item: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock, path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(item, ensure_ascii=True, allow_nan=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def write_json(path: Path, item):
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(item, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        temporary.replace(path)

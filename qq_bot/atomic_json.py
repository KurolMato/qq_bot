"""Bounded, atomic JSON replacement; callers should run this in a worker thread."""
import json
import os
import tempfile
import time
from pathlib import Path


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.stem + "-", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=True)
        for attempt in range(5):
            try:
                temporary.replace(path)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(.05 * (2 ** attempt))
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass

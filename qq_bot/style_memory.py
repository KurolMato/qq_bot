"""Lightweight style memory: sample chats for a while, summarise once or twice.

Not model training — a small JSON profile that is appended to prompts so the
bot gradually matches how a group (or the owner) actually talks.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path

MAX_POINTS = 8
MAX_SAMPLE_CHARS = 200
_sample_lock = threading.RLock()


def _sample_lines(path: Path) -> list[str]:
    # Decode each record separately: one damaged line must not hide good samples.
    with _sample_lock:
        try:
            raw_lines = path.read_bytes().splitlines()
        except OSError:
            return []
    lines = []
    for raw in raw_lines:
        try:
            line = raw.decode("utf-8")
            record = json.loads(line)
            if not isinstance(record, dict):
                continue
            float(record.get("ts", 0))
        except (ValueError, TypeError, OverflowError):
            continue
        lines.append(line)
    return lines


def append_sample(
    path: Path,
    scope: str,
    text: str,
    *,
    max_samples: int = 600,
    now: float | None = None,
) -> None:
    text = " ".join(text.split())[:MAX_SAMPLE_CHARS]
    if not text:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"ts": int(now if now is not None else time.time()), "scope": scope, "text": text}
    with _sample_lock:
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        _compact(path, max_samples)


def _compact(path: Path, max_samples: int) -> None:
    lines = _sample_lines(path)
    if len(lines) <= max_samples * 2:
        return
    by_scope: dict[str, list[str]] = {}
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        by_scope.setdefault(str(record.get("scope", "")), []).append(line)
    kept: list[str] = []
    for items in by_scope.values():
        kept.extend(items[-max_samples:])
    # A failed rewrite must leave the original file intact.
    fd, name = tempfile.mkstemp(prefix=path.stem + "-", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write("\n".join(kept) + "\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def scopes_in_samples(path: Path) -> set[str]:
    lines = _sample_lines(path)
    scopes: set[str] = set()
    for line in lines:
        try:
            scopes.add(str(json.loads(line).get("scope", "")))
        except ValueError:
            continue
    scopes.discard("")
    return scopes


def load_samples(path: Path, scope: str, *, since: float = 0, limit_chars: int = 3000) -> str:
    lines = _sample_lines(path)
    picked: list[str] = []
    total = 0
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if str(record.get("scope")) != scope:
            continue
        if float(record.get("ts", 0)) < since:
            continue
        text = str(record.get("text") or "")
        if not text:
            continue
        if total + len(text) > limit_chars:
            break
        picked.append(text)
        total += len(text)
    return "\n".join(picked)


def parse_style_points(answer: str) -> list[str]:
    """Read a JSON array (or bullet lines) of short style notes from the model."""
    match = re.search(r"\[[\s\S]*?\]", answer)
    items: list[str] = []
    if match:
        try:
            raw = json.loads(match.group(0))
        except ValueError:
            raw = None
        if isinstance(raw, list):
            items = [str(item) for item in raw if isinstance(item, (str, int, float))]
    if not items:
        for line in answer.splitlines():
            if not re.match(r"^\s*(?:[-*•]|\d+[.、)])", line):
                continue
            cleaned = re.sub(r"^\s*(?:[-*•]|\d+[.、)])\s*", "", line).strip()
            if cleaned and len(cleaned) <= 30:
                items.append(cleaned)
    cleaned_items: list[str] = []
    for item in items:
        value = " ".join(str(item).split()).strip("，。；; ")
        if value and len(value) <= 30 and value not in cleaned_items:
            cleaned_items.append(value)
    return cleaned_items[:6]


def merge_points(existing: list[str], new: list[str], cap: int = MAX_POINTS) -> list[str]:
    merged: list[str] = []
    for item in [*existing, *new]:
        value = " ".join(str(item).split()).strip()
        if value and value not in merged:
            merged.append(value)
    return merged[-cap:]


def load_styles(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"scopes": {}}
    if not isinstance(raw, dict) or not isinstance(raw.get("scopes"), dict):
        return {"scopes": {}}
    return raw


def save_styles(path: Path, data: dict) -> None:
    from .atomic_json import write_json

    write_json(path, data)


def style_hint(styles: dict, scope: str) -> str:
    entry = (styles.get("scopes") or {}).get(scope) or {}
    points = entry.get("points") or []
    if not points:
        return ""
    joined = "；".join(str(point) for point in points[:8])
    return f"\n\n（这个群/这个人平时说话的风格：{joined}。自然贴合，别刻意模仿。）"

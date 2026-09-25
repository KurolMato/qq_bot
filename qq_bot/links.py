from __future__ import annotations

import re
from html import unescape
from collections.abc import Iterable, Mapping
from typing import Any

from app.downloader import normalize_bilibili_url, normalize_video_url


URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
TRAILING = "，。！？；：、,.!?;:)]}）】》>"


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for child in value.values():
            yield from _strings(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _strings(child)


def extract_video_urls(message: Any) -> list[str]:
    """Extract supported video links from text and OneBot share data."""
    candidates: list[str] = []
    for text in _strings(message):
        decoded = unescape(text).replace(r"\/", "/")
        candidates.extend(match.rstrip(TRAILING) for match in URL_RE.findall(decoded))

    result: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            normalized = normalize_video_url(candidate)
        except ValueError:
            continue
        if normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def extract_bilibili_urls(message: Any) -> list[str]:
    """Backward-compatible Bilibili-only extractor."""
    result: list[str] = []
    for url in extract_video_urls(message):
        try:
            result.append(normalize_bilibili_url(url))
        except ValueError:
            pass
    return result

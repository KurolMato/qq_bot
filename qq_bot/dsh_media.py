"""Media helpers for the QQ ⇄ DSH bridge (no NoneBot imports, easy to test).

Two directions:

* inbound — turn OneBot image segments into ``ImagePart`` bytes the ACP client
  can send to the agent, so screenshots and stickers are actually visible;
* outbound — let the agent ask for a sticker by writing ``[[表情:分类]]`` in its
  reply, then pick a random file from ``data/stickers/<分类>/``.
"""

from __future__ import annotations

import asyncio
import hashlib
import random
import re
from dataclasses import dataclass
from pathlib import Path

from .dsh_policy import STICKER_SEND_PROBABILITY

STICKER_MARKER = re.compile(
    r"[\[［]{2}\s*(?:表情|sticker)\s*[:：]\s*([^\]］\s]{1,60})\s*[\]］]{2}",
    re.IGNORECASE,
)
SAVE_MARKER = re.compile(
    r"[\[［]{2}\s*(?:存表情|保存表情|收藏表情|save)\s*[:：]\s*([^\]］\s]{1,12})\s*[\]］]{2}",
    re.IGNORECASE,
)
# Anything that still looks like a sticker marker must never reach the user,
# even when the model writes it slightly wrong (no colon, full-width brackets).
SAVE_URL_MARKER = re.compile(
    r"[\[［]{2}\s*(?:收图|存图|存表情图)\s*[:：]\s*([^|｜\]］\s]{1,12})\s*[|｜]\s*(https?://[^\]］\s]+)\s*[\]］]{2}",
    re.IGNORECASE,
)
STRAY_STICKER = re.compile(
    r"[\[［]{2}[^\]］\n]{0,60}?(?:表情|sticker)[^\]］\n]{0,60}?[\]］]{2}",
    re.IGNORECASE,
)
ALLOWED_STICKER_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
MAX_STICKER_BYTES = 4 * 1024 * 1024
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_STICKERS_PER_CATEGORY = 300
_MIME_SUFFIX = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}


def safe_category(raw: str) -> str | None:
    """Normalise a sticker category name, rejecting anything path-like."""
    category = raw.strip().strip("[]【】")
    if not category or len(category) > 12:
        return None
    if category in {".", ".."} or "/" in category or "\\" in category:
        return None
    if any(ord(char) < 32 for char in category):
        return None
    return category


def _clean_marker_text(text: str) -> str:
    """Normalise whitespace left behind after removing action markers."""
    text = re.sub(r"[ \t]{2,}", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def split_save_markers(text: str) -> tuple[str, list[str]]:
    """Remove save markers from a reply; return the categories to store into."""
    categories = [match.group(1) for match in SAVE_MARKER.finditer(text)]
    cleaned = SAVE_MARKER.sub("", text)
    return _clean_marker_text(cleaned), categories[:1]


def save_sticker(root: Path, category: str, data: bytes) -> Path | None:
    """Store one image as a sticker; None when invalid, duplicate, or over quota."""
    name = safe_category(category)
    if name is None:
        return None
    mime = sniff_image_mime(data)
    if mime is None or not data or len(data) > MAX_STICKER_BYTES:
        return None
    folder = root / name
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    digest = hashlib.sha256(data).hexdigest()[:16]
    destination = folder / f"{digest}{_MIME_SUFFIX[mime]}"
    if destination.exists():
        return None
    existing = [path for path in folder.iterdir() if path.suffix.lower() in ALLOWED_STICKER_SUFFIXES]
    if len(existing) >= MAX_STICKERS_PER_CATEGORY:
        return None
    temporary = folder / f".{digest}.tmp"
    try:
        temporary.write_bytes(data)
        temporary.replace(destination)
    except OSError:
        temporary.unlink(missing_ok=True)
        return None
    return destination


def split_sticker_markers(text: str) -> tuple[str, list[str]]:
    """Remove sticker markers from a reply and return the categories asked for."""
    categories = [match.group(1) for match in STICKER_MARKER.finditer(text)]
    cleaned = STICKER_MARKER.sub("", text)
    cleaned = STRAY_STICKER.sub("", cleaned)
    return _clean_marker_text(cleaned), categories[:1]


def split_save_url_markers(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Remove ``[[收图:分类|url]]`` markers; return (clean text, [(category, url)])."""
    items = [(match.group(1), match.group(2)) for match in SAVE_URL_MARKER.finditer(text)]
    cleaned = SAVE_URL_MARKER.sub("", text)
    cleaned = STRAY_STICKER.sub("", cleaned)
    return _clean_marker_text(cleaned), items[:2]


def is_safe_image_url(url: str) -> bool:
    """Reject non-http(s) and obviously local targets before fetching."""
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    host = parsed.hostname.lower()
    if host in {"localhost", "127.0.0.1", "0.0.0.0", "::1"}:
        return False
    if host.startswith(("10.", "127.", "192.168.", "169.254.", "172.16.", "172.17.",
                        "172.18.", "172.19.", "172.20.", "172.21.", "172.22.", "172.23.",
                        "172.24.", "172.25.", "172.26.", "172.27.", "172.28.", "172.29.",
                        "172.30.", "172.31.")):
        return False
    return True


def sticker_root(project_root: Path) -> Path:
    return project_root / "data" / "stickers"


def pick_sticker(root: Path, category: str, rng: random.Random | None = None) -> Path | None:
    """Pick one sticker from ``root/<category>``; None when the category is empty."""
    category = category.strip().strip("[]【】")
    if not category or "/" in category or "\\" in category or category in {".", ".."}:
        return None
    folder = (root / category).resolve()
    try:
        folder.relative_to(root.resolve())
    except ValueError:
        return None
    if not folder.is_dir():
        return None
    files = [
        path
        for path in folder.iterdir()
        if path.is_file()
        and path.suffix.lower() in ALLOWED_STICKER_SUFFIXES
        and 0 < path.stat().st_size <= MAX_STICKER_BYTES
    ]
    if not files:
        return None
    return (rng or random).choice(sorted(files))


def sticker_categories(root: Path) -> list[str]:
    """Categories that actually contain a usable sticker file."""
    categories: list[str] = []
    try:
        folders = [path for path in root.iterdir() if path.is_dir()]
    except OSError:
        return []
    for folder in folders:
        try:
            usable = any(
                path.is_file()
                and path.suffix.lower() in ALLOWED_STICKER_SUFFIXES
                and 0 < path.stat().st_size <= MAX_STICKER_BYTES
                for path in folder.iterdir()
            )
        except OSError:
            usable = False
        if usable:
            categories.append(folder.name)
    return sorted(categories)


def sticker_hint(root: Path) -> str:
    """Context line that tells the model which sticker categories exist."""
    cats = sticker_categories(root)
    if not cats:
        return ""
    joined = "、".join(cats)
    return (
        f"\n\n（可用表情只有这些分类：{joined}。要发就写 [[表情:分类]]，"
        "分类必须是其中之一，不确定就照抄一个。先写文字回复，表情标记放最后；"
        f"不要只回复表情。每轮普通文字回复发完后，客户端会以 {round(STICKER_SEND_PROBABILITY * 100)}% 的概率附带一张 QQ 表情包；"
        "优先采用你选择的表情，没有选择或文件不可用时从现有表情库补选，不需要解释补选过程）"
    )


def resolve_sticker(root: Path, value: str) -> Path | None:
    """Resolve ``分类/文件名`` to one exact sticker, or ``分类`` to a random one."""
    raw = value.strip().strip("[]【】")
    if not raw:
        return None
    if "/" in raw or "\\" in raw:
        base = root.resolve()
        try:
            candidate = (base / raw.replace("\\", "/")).resolve()
            candidate.relative_to(base)
        except (ValueError, OSError):
            return None
        if (
            candidate.is_file()
            and candidate.suffix.lower() in ALLOWED_STICKER_SUFFIXES
            and 0 < candidate.stat().st_size <= MAX_STICKER_BYTES
        ):
            return candidate
        return None
    return pick_sticker(root, raw)


async def read_sticker(root: Path, category: str) -> bytes | None:
    """Resolve and read a sticker, treating missing/unreadable files as absent."""
    path = resolve_sticker(root, category)
    if path is None:
        return None
    try:
        return await asyncio.to_thread(path.read_bytes)
    except OSError:
        return None


def sniff_image_mime(data: bytes) -> str | None:
    """Return a supported mime type from magic bytes, or None."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


@dataclass
class IncomingImage:
    data: bytes
    mime: str
    source: str = ""


def is_probably_image_url(url: str) -> bool:
    lowered = url.split("?")[0].lower()
    return lowered.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"))

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from nonebot.adapters.onebot.v11 import Message, MessageSegment
from PIL import Image, ImageDraw, ImageFont, ImageOps

from .switch_presence import _card_backdrop, _download_image, _fitted_text, _font


logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Keep using the cache created by the earlier list renderer so existing profile
# avatars appear immediately after this UI upgrade. Game icons share it safely
# because filenames are hashes of their complete URLs.
CACHE_DIR = PROJECT_ROOT / "data" / "list_avatar_cache"
CACHE_MAX_FILES = max(int(os.getenv("LIST_IMAGE_CACHE_MAX_FILES", "512")), 32)
CACHE_MAX_AGE_SECONDS = max(float(os.getenv("LIST_IMAGE_CACHE_MAX_AGE_DAYS", "30")), 1.0) * 86400


@dataclass(frozen=True)
class ListCardEntry:
    name: str
    current_game: str | None
    avatar_url: str | None = None
    game_image_url: str | None = None


PLATFORM_STYLES = {
    "switch": ("Switch", "#e60012", "#ff5964"),
    "ps": ("PS", "#006fcd", "#48a8ff"),
    "steam": ("Steam", "#0b5f91", "#66c0f4"),
    "xbox": ("Xbox", "#107c10", "#52b043"),
}
ONLINE_PLATFORM_ORDER = ("steam", "ps", "switch", "xbox")


def _cache_path(url: str) -> Path:
    return CACHE_DIR / f"{hashlib.sha256(url.encode('utf-8')).hexdigest()}.img"


def _valid_image(content: bytes) -> bool:
    try:
        with Image.open(BytesIO(content)) as image:
            image.verify()
        return True
    except Exception:
        return False


def _prune_cache(keep: Path) -> None:
    try:
        files = [path for path in CACHE_DIR.iterdir() if path.is_file()]
    except OSError:
        return
    cutoff = time.time() - CACHE_MAX_AGE_SECONDS
    entries: list[tuple[float, Path]] = []
    for path in files:
        try:
            modified = path.stat().st_mtime
            if path != keep and modified < cutoff:
                path.unlink(missing_ok=True)
            else:
                entries.append((modified, path))
        except OSError:
            continue
    others = sorted(
        (item for item in entries if item[1] != keep),
        key=lambda item: item[0],
        reverse=True,
    )
    for _, path in others[max(CACHE_MAX_FILES - 1, 0):]:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


async def _load_image(url: str | None, semaphore: asyncio.Semaphore) -> bytes | None:
    if not url:
        return None
    path = _cache_path(url)
    if path.is_file():
        try:
            content = await asyncio.to_thread(path.read_bytes)
            if await asyncio.to_thread(_valid_image, content):
                return content
        except OSError:
            pass

    async with semaphore:
        try:
            # Nintendo's eShop image CDN can take noticeably longer to send a
            # 1-3 MB cover from mainland networks. Keep the fast timeout for
            # avatars/other stores, but allow this known CDN enough time.
            timeout = 18 if "img-eshop.cdn.nintendo.net" in url.casefold() else 6
            content = await asyncio.wait_for(_download_image(url), timeout=timeout)
            if not content or not await asyncio.to_thread(_valid_image, content):
                return None
            await asyncio.to_thread(CACHE_DIR.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(path.write_bytes, content)
            await asyncio.to_thread(_prune_cache, path)
            return content
        except Exception as exc:
            logger.info("List card image unavailable: %s (%s)", url, type(exc).__name__)
            return None


async def _load_assets(entries: list[ListCardEntry]) -> dict[str, bytes | None]:
    urls = {
        url
        for entry in entries
        for url in (entry.avatar_url, entry.game_image_url)
        if url
    }
    if not urls:
        return {}
    semaphore = asyncio.Semaphore(12)
    tasks = {asyncio.create_task(_load_image(url, semaphore)): url for url in urls}
    global_timeout = (
        19
        if any("img-eshop.cdn.nintendo.net" in url.casefold() for url in urls)
        else 7
    )
    done, pending = await asyncio.wait(tasks, timeout=global_timeout)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    result: dict[str, bytes | None] = {url: None for url in urls}
    for task in done:
        try:
            result[tasks[task]] = task.result()
        except Exception:
            result[tasks[task]] = None
    return result


def _source_image(content: bytes | None, size: tuple[int, int], colour: str) -> Image.Image:
    if content:
        try:
            with Image.open(BytesIO(content)) as source:
                return ImageOps.fit(
                    ImageOps.exif_transpose(source).convert("RGB"),
                    size,
                    method=Image.Resampling.LANCZOS,
                    centering=(0.5, 0.5),
                )
        except Exception:
            pass
    return Image.new("RGB", size, colour)


def _paste_rounded(
    canvas: Image.Image,
    source: Image.Image,
    position: tuple[int, int],
    radius: int,
    *,
    circle: bool = False,
) -> None:
    mask = Image.new("L", source.size, 0)
    mask_draw = ImageDraw.Draw(mask)
    if circle:
        mask_draw.ellipse((0, 0, source.width - 1, source.height - 1), fill=255)
    else:
        mask_draw.rounded_rectangle(
            (0, 0, source.width - 1, source.height - 1), radius=radius, fill=255
        )
    canvas.paste(source, position, mask)


def _centred_text(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    value: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    colour: str,
) -> None:
    bounds = draw.textbbox((0, 0), value, font=font)
    width = bounds[2] - bounds[0]
    height = bounds[3] - bounds[1]
    left, top, right, bottom = box
    draw.text(
        (left + (right - left - width) / 2, top + (bottom - top - height) / 2 - bounds[1]),
        value,
        font=font,
        fill=colour,
    )


def _render_fallback_mark(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    value: str,
    colour: str,
) -> None:
    initial = (value.strip() or "?")[0].upper()
    _centred_text(draw, box, initial, _font(34, bold=True), colour)


def render_list_card(
    platform: str,
    entries: list[ListCardEntry],
    assets: dict[str, bytes | None] | None = None,
) -> bytes:
    """Render every subscription into one vertically stacked PNG."""
    assets = assets or {}
    label, accent, accent_light = PLATFORM_STYLES.get(platform.casefold(), PLATFORM_STYLES["switch"])
    width = 1000
    padding = 18
    row_height = 146
    gap = 12
    height = padding * 2 + len(entries) * row_height + max(len(entries) - 1, 0) * gap
    canvas = Image.new("RGB", (width, max(height, 182)), "#09111a")
    draw = ImageDraw.Draw(canvas)

    for index, entry in enumerate(entries):
        top = padding + index * (row_height + gap)
        bottom = top + row_height
        draw.rounded_rectangle(
            (padding, top, width - padding, bottom),
            radius=26,
            fill="#141f2b",
            outline="#2a3c4d",
            width=2,
        )
        draw.rounded_rectangle((padding, top, padding + 9, bottom), radius=5, fill=accent)

        avatar_box = (40, top + 25, 136, top + 121)
        avatar_content = assets.get(entry.avatar_url or "")
        avatar = _source_image(avatar_content, (96, 96), "#26394b")
        _paste_rounded(canvas, avatar, avatar_box[:2], 48, circle=True)
        if not avatar_content:
            _render_fallback_mark(draw, avatar_box, entry.name, "#d9e7f4")

        name, name_font = _fitted_text(draw, entry.name, 238, 28, 17)
        draw.text((158, top + 34), name, font=name_font, fill="#f5f8fc")

        playing = bool(entry.current_game)
        status_box = (158, top + 83, 274, top + 116)
        status_fill = "#227b55" if playing else "#324252"
        status_text = "正在游戏" if playing else "未在游戏"
        draw.rounded_rectangle(status_box, radius=17, fill=status_fill)
        draw.ellipse((171, top + 96, 179, top + 104), fill="#62e3a7" if playing else "#8796a4")
        draw.text((187, top + 88), status_text, font=_font(16), fill="#e5f8ef")

        platform_width = 82 if label in {"Switch", "Steam"} else 68 if label == "Xbox" else 58
        platform_box = (286, top + 83, 286 + platform_width, top + 116)
        draw.rounded_rectangle(platform_box, radius=17, fill=accent)
        _centred_text(draw, platform_box, label, _font(16, bold=True), "#ffffff")

        game_panel = (398, top + 18, width - 38, bottom - 18)
        draw.rounded_rectangle(game_panel, radius=20, fill="#0f1822")

        icon_box = (416, top + 34, 494, top + 112)
        icon_content = assets.get(entry.game_image_url or "") if playing else None
        icon = _source_image(icon_content, (78, 78), "#243648")
        _paste_rounded(canvas, icon, icon_box[:2], 14)
        if not icon_content:
            # A compact controller-like symbol keeps empty/offline rows intentional.
            draw.rounded_rectangle((435, top + 61, 476, top + 88), radius=10, outline=accent_light, width=3)
            draw.line((443, top + 74, 453, top + 74), fill=accent_light, width=3)
            draw.line((448, top + 69, 448, top + 79), fill=accent_light, width=3)
            draw.ellipse((463, top + 70, 468, top + 75), fill=accent_light)
            draw.ellipse((470, top + 77, 475, top + 82), fill=accent_light)

        game_name = entry.current_game or "未在游戏"
        game_text, game_font = _fitted_text(draw, game_name, 436, 27, 17)
        bounds = draw.textbbox((0, 0), game_text, font=game_font)
        text_height = bounds[3] - bounds[1]
        draw.text(
            (516, top + (row_height - text_height) / 2 - bounds[1]),
            game_text,
            font=game_font,
            fill="#ffffff" if playing else "#8fa0af",
        )

    output = BytesIO()
    canvas.save(output, format="PNG", optimize=True)
    return output.getvalue()


async def build_list_message(platform: str, entries: list[ListCardEntry]) -> Message:
    assets = await _load_assets(entries)
    card = await asyncio.to_thread(render_list_card, platform, entries, assets)
    return Message(MessageSegment.image(card, cache=False, proxy=False, timeout=30))


def render_online_overview(
    groups: dict[str, list[ListCardEntry]],
    assets: dict[str, bytes | None] | None = None,
    unhealthy_platforms: set[str] | None = None,
) -> bytes:
    """Render currently playing members in four fixed platform columns."""
    assets = assets or {}
    unhealthy_platforms = unhealthy_platforms or set()
    width = 1560
    padding = 24
    column_gap = 14
    column_width = (width - padding * 2 - column_gap * 3) // 4
    title_height = 104
    header_height = 58
    row_height = 126
    row_gap = 12
    max_rows = max(
        (0 if key in unhealthy_platforms else len(groups.get(key, []))
         for key in ONLINE_PLATFORM_ORDER),
        default=0,
    )
    visible_rows = max(max_rows, 1)
    height = (
        padding
        + title_height
        + header_height
        + 14
        + visible_rows * row_height
        + max(visible_rows - 1, 0) * row_gap
        + padding
    )
    canvas = Image.new("RGB", (width, height), "#08121c")
    draw = ImageDraw.Draw(canvas)
    total = sum(
        len(groups.get(key, []))
        for key in ONLINE_PLATFORM_ORDER
        if key not in unhealthy_platforms
    )
    draw.text((padding, 24), "当前正在游玩", font=_font(36, bold=True), fill="#f3f8fc")
    draw.text((padding, 72), f"四平台共 {total} 位玩家", font=_font(18), fill="#91a7ba")

    for column_index, platform in enumerate(ONLINE_PLATFORM_ORDER):
        label, accent, accent_light = PLATFORM_STYLES[platform]
        left = padding + column_index * (column_width + column_gap)
        right = left + column_width
        header_top = padding + title_height
        draw.rounded_rectangle(
            (left, header_top, right, header_top + header_height),
            radius=20,
            fill=accent,
        )
        _centred_text(
            draw,
            (left, header_top, right, header_top + header_height),
            label,
            _font(22, bold=True),
            "#ffffff",
        )

        entries = groups.get(platform, [])
        rows_top = header_top + header_height + 14
        if platform in unhealthy_platforms:
            bottom = rows_top + row_height
            draw.rounded_rectangle(
                (left, rows_top, right, bottom),
                radius=22,
                fill="#25171a",
                outline="#8f3541",
                width=2,
            )
            _centred_text(
                draw,
                (left, rows_top, right, bottom),
                "异常",
                _font(22, bold=True),
                "#ff7180",
            )
            continue
        if not entries:
            bottom = rows_top + row_height
            draw.rounded_rectangle(
                (left, rows_top, right, bottom),
                radius=22,
                fill="#121e29",
                outline="#263a4b",
                width=2,
            )
            _centred_text(
                draw,
                (left, rows_top, right, bottom),
                "暂无正在游玩",
                _font(18),
                "#758a9c",
            )
            continue

        for row_index, entry in enumerate(entries):
            top = rows_top + row_index * (row_height + row_gap)
            bottom = top + row_height
            cover_content = assets.get(entry.game_image_url or "")
            background = _card_backdrop(
                cover_content,
                (right - left, bottom - top),
                shade=112,
            )
            if background is not None:
                _paste_rounded(canvas, background, (left, top), 22)
                draw.rounded_rectangle(
                    (left, top, right, bottom),
                    radius=22,
                    outline="#49647b",
                    width=2,
                )
            else:
                draw.rounded_rectangle(
                    (left, top, right, bottom),
                    radius=22,
                    fill="#14212d",
                    outline="#2b4051",
                    width=2,
                )
            draw.rounded_rectangle((left, top, left + 7, bottom), radius=4, fill=accent)

            avatar_box = (left + 16, top + 19, left + 78, top + 81)
            avatar_content = assets.get(entry.avatar_url or "")
            avatar = _source_image(avatar_content, (62, 62), "#293d4e")
            _paste_rounded(canvas, avatar, avatar_box[:2], 31, circle=True)
            if not avatar_content:
                _render_fallback_mark(draw, avatar_box, entry.name, "#dce9f3")

            cover_box = (right - 91, top + 20, right - 17, top + 106)
            cover = _source_image(cover_content, (74, 86), "#243748")
            _paste_rounded(canvas, cover, cover_box[:2], 13)
            if not cover_content:
                _render_fallback_mark(draw, cover_box, "游", accent_light)

            text_left = left + 92
            text_width = max(80, cover_box[0] - text_left - 12)
            name, name_font = _fitted_text(draw, entry.name, text_width, 22, 15)
            draw.text((text_left, top + 24), name, font=name_font, fill="#f4f8fb")
            draw.ellipse((text_left, top + 61, text_left + 8, top + 69), fill=accent_light)
            game, game_font = _fitted_text(
                draw,
                entry.current_game or "未知游戏",
                text_width - 16,
                18,
                13,
            )
            draw.text((text_left + 16, top + 56), game, font=game_font, fill="#cbd8e3")

    output = BytesIO()
    canvas.save(output, format="PNG", optimize=True)
    return output.getvalue()


async def build_online_overview_message(
    groups: dict[str, list[ListCardEntry]],
    unhealthy_platforms: set[str] | None = None,
) -> Message:
    unhealthy_platforms = unhealthy_platforms or set()
    entries = [
        entry
        for key in ONLINE_PLATFORM_ORDER
        if key not in unhealthy_platforms
        for entry in groups.get(key, [])
    ]
    assets = await _load_assets(entries)
    card = await asyncio.to_thread(
        render_online_overview, groups, assets, unhealthy_platforms
    )
    return Message(MessageSegment.image(card, cache=False, proxy=False, timeout=30))

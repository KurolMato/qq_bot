"""Render the image card used by the bare ``/rank me`` command.

The renderer is deliberately synchronous and self-contained: callers supply
all image bytes, and this module does not fetch assets or read tracker data.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from io import BytesIO
from typing import Any, Mapping, Sequence

from PIL import Image, ImageDraw, ImageOps

from .game_time_tracker import LOCAL_TZ, month_start
from .switch_presence import _fitted_text, _font


BG = "#0C131A"
PANEL = "#151F29"
LINE = "#293642"
WHITE = "#F2F5F7"
MUTED = "#A7BBCD"
MINT = "#A9E7D3"
CANVAS_SIZE = (1500, 1000)
GAME_TILE_SIZE = (324, 182)
MAX_GAME_TILES = 6
DATA_KEYS = (
    "name", "avatar_url", "day_seconds", "week_seconds", "month_seconds", "games",
    "recent_games", "platinum_count", "perfect_count", "achievements", "as_of", "finalized_month",
)


def achievement_icon(kind: str, size: int = 56) -> Image.Image:
    """Draw a silver-blue platinum cup or a gold-and-blue completion ribbon."""
    icon = Image.new("RGBA", (192, 192))
    draw = ImageDraw.Draw(icon)
    if kind == "白金":
        draw.arc((15, 26, 104, 137), 80, 275, fill="#374C77", width=18)
        draw.arc((88, 26, 177, 137), 265, 460, fill="#374C77", width=18)
        draw.arc((19, 23, 102, 133), 80, 275, fill="#BDCEE9", width=9)
        draw.arc((90, 23, 173, 133), 265, 460, fill="#BDCEE9", width=9)
        draw.polygon(
            [(78, 115), (114, 115), (108, 154), (133, 168), (133, 178),
             (59, 178), (59, 168), (84, 154)], fill="#B4C8E3"
        )
        draw.polygon(
            [(90, 120), (102, 120), (101, 160), (121, 170), (75, 170),
             (92, 158)], fill="#F0F6FF"
        )
        draw.ellipse((40, 17, 151, 130), fill="#526FA2", outline="#DEEAFC", width=5)
        draw.ellipse((50, 23, 141, 116), fill="#89ABD3")
        draw.polygon(
            [(96, 25), (126, 49), (119, 85), (89, 119), (100, 77), (74, 55)],
            fill="#EDF8FF",
        )
        draw.polygon(
            [(96, 25), (74, 55), (100, 77), (86, 88), (65, 63)], fill="#BDD8ED"
        )
        draw.polygon([(126, 49), (119, 85), (89, 119), (109, 76)], fill="#5C7DB7")
        draw.arc((55, 24, 136, 113), 145, 270, fill="#F7FAFF", width=5)
    else:
        draw.polygon(
            [(52, 99), (87, 112), (77, 177), (57, 157), (34, 164)],
            fill="#6DC4F1", outline="#285D91", width=5,
        )
        draw.polygon(
            [(105, 111), (140, 99), (159, 164), (132, 157), (113, 177)],
            fill="#6DC4F1", outline="#285D91", width=5,
        )
        points = []
        for index in range(28):
            angle = -math.pi / 2 + index * math.pi / 14
            radius = 69 if index % 2 == 0 else 55
            points.append((96 + math.cos(angle) * radius, 78 + math.sin(angle) * radius))
        draw.polygon(points, fill="#6DC4F1", outline="#285D91", width=5)
        draw.ellipse((53, 35, 139, 121), fill="#FFD42F", outline="#DDA51C", width=5)
        draw.arc((60, 42, 132, 115), 200, 290, fill="#FFE784", width=4)
    return icon.resize((size, size), Image.Resampling.LANCZOS)


def achievement_frame(kind: str, size: tuple[int, int]) -> Image.Image:
    """Draw the preview's transparent, antialiased tile frame."""
    scale = 3
    width, height = size
    big_size = (width * scale, height * scale)
    mask = Image.new("L", big_size)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.rounded_rectangle(
        (0, 0, big_size[0] - 1, big_size[1] - 1), 12 * scale, fill=255
    )
    mask_draw.rounded_rectangle(
        (5 * scale, 5 * scale, big_size[0] - 1 - 5 * scale,
         big_size[1] - 1 - 5 * scale),
        8 * scale, fill=0,
    )
    if kind == "白金":
        stops = [(74, 119, 175), (215, 236, 255), (132, 174, 213),
                 (244, 250, 255), (94, 141, 194)]
    else:
        stops = [(58, 155, 219), (123, 207, 235), (255, 229, 125),
                 (244, 187, 53), (255, 223, 109)]
    ramp = Image.new("RGB", (big_size[0] + big_size[1], 1))
    pixels = []
    for position in range(ramp.width):
        value = position / (ramp.width - 1) * (len(stops) - 1)
        index = min(int(value), len(stops) - 2)
        mix = value - index
        pixels.append(
            tuple(round(a + (b - a) * mix) for a, b in zip(stops[index], stops[index + 1]))
        )
    ramp.putdata(pixels)
    frame = Image.new("RGBA", big_size)
    for y in range(big_size[1]):
        frame.paste(ramp.crop((y, 0, y + big_size[0], 1)), (0, y))
    frame.putalpha(mask)
    frame_draw = ImageDraw.Draw(frame)
    if kind == "白金":
        for points in [
            [(width - 43, 9), (width - 13, 9), (width - 9, 13), (width - 9, 35)],
            [(9, height - 35), (9, height - 13), (13, height - 9), (43, height - 9)],
        ]:
            frame_draw.line(
                [(x * scale, y * scale) for x, y in points], fill="#D9EFFF", width=scale
            )
        cx, cy = (width - 29) * scale, 22 * scale
        frame_draw.polygon(
            [(cx, cy - 6 * scale), (cx + 4 * scale, cy),
             (cx, cy + 6 * scale), (cx - 4 * scale, cy)],
            fill="#F2F9FF",
        )
    else:
        for offset in (0, 7):
            points = [(width - 45 + offset, 9), (width - 19 + offset, 9),
                      (width - 9, 19 - offset)]
            frame_draw.line(
                [(x * scale, y * scale) for x, y in points],
                fill="#FFE694", width=2 * scale,
            )
        frame_draw.line(
            [(10 * scale, (height - 13) * scale),
             (16 * scale, (height - 9) * scale), (44 * scale, (height - 9) * scale)],
            fill="#FFE694", width=2 * scale,
        )
    return frame.resize(size, Image.Resampling.LANCZOS)


def _get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _safe_seconds(value: Any) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return max(seconds, 0.0) if math.isfinite(seconds) else 0.0


def _duration(seconds: Any) -> str:
    """Format seconds consistently, rounding to the nearest minute."""
    total_minutes = max(0, int(round(_safe_seconds(seconds) / 60)))
    hours, minutes = divmod(total_minutes, 60)
    if hours and minutes:
        return f"{hours} 小时 {minutes} 分"
    if hours:
        return f"{hours} 小时"
    return f"{minutes} 分"


def timeout_cover() -> bytes:
    """A deliberate, temporary missing-cover state matching the dashboard."""
    image = Image.new("RGB", (640, 360), "#20323D")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((272, 56, 368, 119), radius=18, outline="#76939E", width=4)
    draw.line((290, 87, 316, 87), fill="#9CBDBF", width=4)
    draw.line((303, 74, 303, 100), fill="#9CBDBF", width=4)
    draw.ellipse((341, 78, 349, 86), fill="#9CBDBF")
    draw.ellipse((329, 91, 337, 99), fill="#9CBDBF")
    draw.text((320, 150), "封面暂未加载", font=_font(26), fill="#B9CDCF", anchor="mt")
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _decoded_asset(content: Any, *, landscape: bool = False) -> Image.Image | None:
    """Decode supplied image bytes and reject corrupt or transparent assets."""
    if not isinstance(content, (bytes, bytearray, memoryview)) or not content:
        return None
    raw = bytes(content)
    try:
        with Image.open(BytesIO(raw)) as probe:
            probe.verify()
        with Image.open(BytesIO(raw)) as source:
            image = ImageOps.exif_transpose(source)
            image.load()
            if image.width < 1 or image.height < 1:
                return None
            if landscape and image.width <= image.height:
                return None
            decoded = image.convert("RGBA")
            if decoded.getchannel("A").getextrema() == (0, 0):
                return None
            return decoded.copy()
    except Exception:
        return None


def _image_for_url(assets: Mapping[str, bytes | None], url: Any, *, landscape: bool = False) -> Image.Image | None:
    if not isinstance(url, str) or not url:
        return None
    return _decoded_asset(assets.get(url), landscape=landscape)


def requires_landscape(game: Any) -> bool:
    return str(_get(game, "platform") or "").casefold() not in {"switch", "nintendo", "ns", "nsw", "nintendoswitch"}


def _fit_game_art(source: Image.Image, size: tuple[int, int], game: Any) -> Image.Image:
    # Fit every platform edge-to-edge without distorting the source aspect ratio.
    return ImageOps.fit(source.convert("RGB"), size, method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))


def _available_month_covers(
    games: Sequence[Any], assets: Mapping[str, bytes | None]
) -> list[tuple[Any, Image.Image]]:
    """Keep only games with a decoded, genuinely landscape cover."""
    cache: dict[tuple[str, bool], Image.Image | None] = {}
    result: list[tuple[Any, Image.Image]] = []
    for game in games:
        url = _get(game, "image_url")
        if not isinstance(url, str) or not url:
            continue
        key = (url, requires_landscape(game))
        if key not in cache:
            cache[key] = _image_for_url(assets, url, landscape=key[1])
        image = cache[key]
        if image is not None:
            result.append((game, image))
    return result


def _rounded_paste(
    canvas: Image.Image, image: Image.Image, position: tuple[int, int], radius: int
) -> None:
    mask = Image.new("L", image.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, image.width - 1, image.height - 1), radius=radius, fill=255
    )
    canvas.paste(image, position, mask)


def _glass_rim(canvas: Image.Image, box: tuple[int, int, int, int], radius: int) -> None:
    """A thin, antialiased reflection without emissive bloom."""
    x, y, right, bottom = box
    width, height = right - x, bottom - y
    scale = 3
    mask = Image.new("L", (width * scale, height * scale))
    edge = ImageDraw.Draw(mask)
    edge.rounded_rectangle((0, 0, mask.width - 1, mask.height - 1), radius * scale,
                           outline=255, width=scale)
    mask = mask.resize((width, height), Image.Resampling.LANCZOS)
    rim = Image.new("RGBA", (width, height))
    draw = ImageDraw.Draw(rim)
    for row in range(height):
        t = row / max(1, height - 1)
        color = tuple(round(a + (b - a) * t) for a, b in
                      zip((103, 132, 157), (48, 70, 90)))
        draw.line((0, row, width, row), fill=(*color, 255))
    canvas.paste(rim, (x, y), mask)


def _glass_panel(canvas: Image.Image, box: tuple[int, int, int, int], radius: int = 30) -> None:
    """Smoked glass with restrained surface reflections on a plain background."""
    x, y, right, bottom = box
    panel = Image.new("RGBA", (right - x, bottom - y))
    draw = ImageDraw.Draw(panel)
    for row in range(panel.height):
        t = row / max(1, panel.height - 1)
        sheen = math.exp(-t * 9) * 0.7 + math.exp(-(1 - t) * 12) * 0.25
        color = tuple(round(a + (b - a) * sheen) for a, b in
                      zip((19, 31, 43), (36, 56, 76)))
        draw.line((0, row, panel.width, row), fill=(*color, 255))
    _rounded_paste(canvas, panel, (x, y), radius)
    _glass_rim(canvas, box, radius)


def _text(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    value: str,
    size: int = 22,
    color: str = WHITE,
    *,
    bold: bool = False,
    anchor: str = "lt",
) -> None:
    draw.text((x, y), value, font=_font(size, bold=bold), fill=color, anchor=anchor)


def _fitted(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    value: str,
    max_width: int,
    start_size: int,
    min_size: int,
    color: str = WHITE,
    *,
    anchor: str = "lt",
) -> None:
    display, font = _fitted_text(draw, value, max_width, start_size, min_size)
    draw.text((x, y), display, font=font, fill=color, anchor=anchor)


def _shade(image: Image.Image, strength: int = 246) -> Image.Image:
    layer = Image.new("RGBA", image.size)
    layer_draw = ImageDraw.Draw(layer)
    for y in range(image.height):
        alpha = int(strength * (y / max(1, image.height - 1)) ** 2)
        layer_draw.line((0, y, image.width, y), fill=(5, 12, 18, alpha))
    return Image.alpha_composite(image.convert("RGBA"), layer)


def _platform_label(value: Any, *, short: bool = False) -> str:
    raw = str(value or "").strip()
    normalized = raw.casefold()
    if normalized in {"steam", "steam_id"}:
        return "Steam"
    if normalized in {"ps", "psn", "ps4", "ps5", "playstation", "playstation 4", "playstation 5"}:
        return "PS" if short else "PlayStation"
    if normalized in {"switch", "nintendo switch"}:
        return "Switch"
    if normalized in {"xbox", "xbox one", "xbox series x|s"}:
        return "Xbox"
    return raw or "未知平台"


def _platforms(games: Sequence[Any]) -> str:
    labels: list[str] = []
    seen: set[str] = set()
    for game in games:
        raw = str(_get(game, "platform", "")).strip()
        if not raw:
            continue
        label = _platform_label(raw)
        key = label.casefold()
        if key not in seen:
            seen.add(key)
            labels.append(label)
    return "  ·  ".join(labels) if labels else "暂无平台数据"


def _game_key(game: Any) -> tuple[str, str]:
    return (str(_get(game, "platform", "")).casefold(), str(_get(game, "name", "")))


def _month_game_seconds(games: Sequence[Any], target: Any) -> float:
    key = _game_key(target)
    return sum(_safe_seconds(_get(game, "seconds", 0)) for game in games if _game_key(game) == key)


def _draw_identity(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    data: Mapping[str, Any],
    games: Sequence[Any],
    assets: Mapping[str, bytes | None],
) -> None:
    _glass_panel(canvas, (48, 112, 372, 455))
    name = str(data.get("name") or "玩家")
    avatar = _image_for_url(assets, data.get("avatar_url"))
    if avatar is not None:
        avatar = ImageOps.fit(
            avatar.convert("RGB"), (204, 204), method=Image.Resampling.LANCZOS
        )
        _rounded_paste(canvas, avatar, (108, 138), 30)
        _glass_rim(canvas, (108, 138, 312, 342), 30)
        name_y, platforms_y = 361, 408
    else:
        # With no valid avatar, move the text block into the open center of the panel.
        name_y, platforms_y = 232, 284
    _fitted(draw, 210, name_y, name, 276, 32, 18, WHITE, anchor="mt")
    _fitted(draw, 210, platforms_y, _platforms(games), 282, 16, 12, MUTED, anchor="mt")


def _draw_hero(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    data: Mapping[str, Any],
    games: Sequence[Any],
    recent_games: Sequence[Any],
    assets: Mapping[str, bytes | None],
) -> None:
    box = (396, 112, 1452, 455)
    recent = max(recent_games, key=lambda game: _safe_seconds(_get(game, "seconds", 0)), default=None)
    cover = _image_for_url(assets, _get(recent, "image_url"), landscape=requires_landscape(recent)) if recent is not None else None
    if cover is not None:
        hero = _fit_game_art(cover, (box[2] - box[0], box[3] - box[1]), recent)
        _rounded_paste(canvas, _shade(hero), (box[0], box[1]), 30)
        _glass_rim(canvas, box, 30)
    else:
        # This is an intentional text-only hero state, never an empty image tile.
        _glass_panel(canvas, box)
    _glass_panel(canvas, (424, 136, 594, 174), 19)
    draw.ellipse((440, 151, 447, 158), fill=MINT)
    _text(draw, 459, 144, "本周常玩", 19, MINT)
    if recent is None:
        _fitted(draw, 426, 326, "本周暂无游戏记录", 700, 34, 24, WHITE)
        _text(draw, 427, 389, "开始游玩后，这里会展示本周常玩游戏", 19, "#C5D0D8")
        return

    game_name = str(_get(recent, "name") or "未知游戏")
    platform = _platform_label(_get(recent, "platform"), short=True)
    recent_seconds = _safe_seconds(_get(recent, "seconds", 0))
    month_seconds = _month_game_seconds(games, recent)
    _fitted(draw, 426, 326, game_name, 670, 35, 21, WHITE)
    _fitted(draw, 427, 389, f"{platform}   /   本周", 460, 19, 14, "#C5D0D8")
    _fitted(draw, 1419, 326, _duration(recent_seconds), 286, 29, 14, WHITE, anchor="rt")
    _fitted(draw, 1419, 389, f"本月累计 {_duration(month_seconds)}", 430, 18, 13, "#C5D0D8", anchor="rt")


def _draw_summary(canvas: Image.Image, draw: ImageDraw.ImageDraw, data: Mapping[str, Any]) -> None:
    _glass_panel(canvas, (48, 479, 372, 958))
    _text(draw, 76, 504, "游玩概览", 23, WHITE, bold=True)
    metrics = (
        ("今日游玩", data.get("day_seconds", 0), 558),
        ("本周游玩", data.get("week_seconds", 0), 652),
        ("本月游玩", data.get("month_seconds", 0), 746),
    )
    for label, seconds, y in metrics:
        _text(draw, 76, y, label, 19, MUTED)
        _fitted(draw, 76, y + 32, _duration(seconds), 268, 31, 18, WHITE)
    draw.line((76, 837, 344, 837), fill=LINE)
    _text(draw, 76, 858, "本月白金 / 全成就", 19, MUTED)
    values = (data.get("platinum_count"), data.get("perfect_count"))
    for center_x, value, label in ((143, values[0], "白金"), (278, values[1], "全成就")):
        display = "—" if value is None else str(max(0, int(value)))
        _draw_count_label(draw, center_x, display, label)


def _draw_count_label(
    draw: ImageDraw.ImageDraw, center_x: int, count: str, label: str
) -> None:
    """Center a fitted count and its label as one measured horizontal group."""
    label_font = _font(18)
    label_box = draw.textbbox((0, 0), label, font=label_font)
    label_width = label_box[2] - label_box[0]
    gap = 7
    group_width = 124
    count_font = _font(33, bold=True)
    count_box = draw.textbbox((0, 0), count, font=count_font)
    count_width = count_box[2] - count_box[0]
    for size in range(33, 8, -1):
        count_font = _font(size, bold=True)
        count_box = draw.textbbox((0, 0), count, font=count_font)
        count_width = count_box[2] - count_box[0]
        if count_width + gap + label_width <= group_width:
            break

    total_width = count_width + gap + label_width
    left = center_x - total_width / 2
    row_top, row_height = 892, 39
    count_height = count_box[3] - count_box[1]
    label_height = label_box[3] - label_box[1]
    count_y = row_top + (row_height - count_height) / 2
    label_x = left + count_width + gap
    label_y = row_top + (row_height - label_height) / 2
    draw.text(
        (round(left - count_box[0]), round(count_y - count_box[1])),
        count, font=count_font, fill=MINT,
    )
    draw.text(
        (round(label_x - label_box[0]), round(label_y - label_box[1])),
        label, font=label_font, fill=WHITE,
    )


def _row_x_positions(count: int) -> list[int]:
    tile_width = GAME_TILE_SIZE[0]
    stride = tile_width + 14
    row_width = count * tile_width + max(0, count - 1) * 14
    start = 424 + (1000 - row_width) // 2
    return [start + index * stride for index in range(count)]


def _tile_positions(count: int) -> list[tuple[int, int]]:
    if count <= 3:
        # Center short one-row collages vertically and horizontally.
        x_positions = _row_x_positions(count)
        y = 554 + (376 - GAME_TILE_SIZE[1]) // 2
        return [(x, y) for x in x_positions]
    first_row = _row_x_positions(3)
    second_count = count - 3
    second_row = _row_x_positions(second_count)
    return [(x, 554) for x in first_row] + [(x, 746) for x in second_row]


def _draw_game_tile(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    game: Any,
    source: Image.Image,
    position: tuple[int, int],
    achievement: str | None,
) -> None:
    x, y = position
    tile = _fit_game_art(source, GAME_TILE_SIZE, game)
    _rounded_paste(canvas, _shade(tile, 252), position, 12)
    _glass_rim(canvas, (x, y, x + tile.width, y + tile.height), 12)
    if achievement in {"白金", "全成就"}:
        frame = achievement_frame(achievement, tile.size)
        # Keep shared achievement artwork unchanged for other card renderers.
        frame.putalpha(frame.getchannel("A").point(lambda alpha: round(alpha * 0.72)))
        canvas.paste(frame, position, frame)
        badge = achievement_icon(achievement)
        canvas.paste(badge, (x + 12, y + 10), badge)
    title = str(_get(game, "name") or "未知游戏")
    _fitted(draw, x + 15, y + 112, title, 294, 21, 13, WHITE)
    platform = _platform_label(_get(game, "platform"), short=True)
    _fitted(draw, x + 15, y + 150, platform, 165, 16, 12, "#C5D0D8")
    _fitted(draw, x + 309, y + 149, _duration(_get(game, "seconds", 0)), 112, 17, 12, WHITE, anchor="rt")


def _draw_collage(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    data: Mapping[str, Any],
    games: Sequence[Any],
    assets: Mapping[str, bytes | None],
) -> None:
    _glass_panel(canvas, (396, 479, 1452, 958))
    _text(draw, 424, 503, "本月游戏拼图", 24, WHITE, bold=True)
    available = _available_month_covers(games, assets)
    visible = available[:MAX_GAME_TILES]
    omitted = max(0, len(games) - len(visible))
    summary = f"共 {len(games)} 款游戏 · 本月累计 {_duration(data.get('month_seconds', 0))}"
    if omitted:
        summary += f" · 展示 {len(visible)} 张封面 · 另有 {omitted} 款未展示"
    _fitted(draw, 1424, 508, summary, 758, 18, 12, MUTED, anchor="rt")

    if not visible:
        if games:
            message = "本月暂无可用的横版封面"
            detail = "游戏时长仍计入本月累计"
        else:
            message = "本月暂无游戏记录"
            detail = "有新记录时，这里会展示本月游戏"
        _text(draw, 924, 675, message, 23, WHITE, anchor="mt")
        _text(draw, 924, 715, detail, 17, MUTED, anchor="mt")
        return

    achievements = data.get("achievements")
    if not isinstance(achievements, Mapping):
        achievements = {}
    positions = _tile_positions(len(visible))
    for (game, cover), position in zip(visible, positions):
        kind = achievements.get(_game_key(game))
        _draw_game_tile(
            canvas, draw, game, cover, position,
            kind if kind in {"白金", "全成就"} else None,
        )


def render_personal_rank(data: Any, assets: Mapping[str, bytes | None]) -> bytes:
    """Return a PNG for one player's personal day/week/month gaming summary.

    ``assets`` maps avatar and game image URLs to their already-loaded bytes.
    Invalid/missing covers are omitted, and only decoded landscape covers are
    used for the hero and month collage.
    """
    if not isinstance(data, Mapping):
        data = {key: getattr(data, key, None) for key in DATA_KEYS}
    assets = assets if isinstance(assets, Mapping) else {}
    raw_games = data.get("games", ())
    games = tuple(raw_games) if isinstance(raw_games, (list, tuple)) else ()
    raw_recent = data.get("recent_games", ())
    recent_games = tuple(raw_recent) if isinstance(raw_recent, (list, tuple)) else ()

    canvas = Image.new("RGBA", CANVAS_SIZE, BG)
    draw = ImageDraw.Draw(canvas)
    as_of = data.get("as_of")
    if isinstance(as_of, datetime):
        if as_of.tzinfo is None:
            as_of = as_of.replace(tzinfo=timezone.utc)
        local_as_of = as_of.astimezone(LOCAL_TZ)
        stamp = (month_start(as_of).strftime("%Y年%m月 · 月末结算")
                 if data.get("finalized_month") else local_as_of.strftime("%Y.%m.%d"))
        _text(draw, 1452, 43, stamp, 18, MUTED, anchor="rt")
    _draw_identity(canvas, draw, data, games, assets)
    _draw_hero(canvas, draw, data, games, recent_games, assets)
    _draw_summary(canvas, draw, data)
    _draw_collage(canvas, draw, data, games, assets)
    output = BytesIO()
    canvas.convert("RGB").save(output, format="PNG", optimize=True)
    return output.getvalue()

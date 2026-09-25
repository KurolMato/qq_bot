from __future__ import annotations

import asyncio
import logging
from io import BytesIO

from nonebot.adapters.onebot.v11 import Message, MessageSegment
from PIL import Image, ImageDraw, ImageFilter

from .game_time_tracker import LeaderboardSnapshot, TrackedGame, TrackedMember
from .list_cards import (
    ListCardEntry,
    _load_assets,
    _paste_rounded,
    _render_fallback_mark,
    _source_image,
)
from .switch_presence import _fitted_text, _font


logger = logging.getLogger(__name__)
PLATFORM = {
    "switch": ("Switch", "#e60012"),
    "ps": ("PS", "#006fcd"),
    "ps5": ("PS", "#006fcd"),
    "steam": ("Steam", "#0b5f91"),
    "xbox": ("Xbox", "#107c10"),
}
RANK_COLOURS = ("#ffd15c", "#b8cad9", "#d79562")
MAX_IMAGE_BYTES = 2_000_000



def _centred(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], text: str, font, fill: str) -> None:
    bounds = draw.textbbox((0, 0), text, font=font)
    width = bounds[2] - bounds[0]
    height = bounds[3] - bounds[1]
    left, top, right, bottom = box
    draw.text(
        (left + (right - left - width) / 2, top + (bottom - top - height) / 2 - bounds[1]),
        text,
        font=font,
        fill=fill,
    )


def _minutes(seconds: float) -> int:
    return max(1, int(round(seconds / 60)))


def _duration(seconds: float) -> str:
    minutes = _minutes(seconds)
    hours, remainder = divmod(minutes, 60)
    if hours and remainder:
        return f"{hours}小时{remainder}分"
    if hours:
        return f"{hours}小时"
    return f"{remainder}分钟"


def _leaderboard_cover(content: bytes | None, size: tuple[int, int]) -> Image.Image:
    """Use the same centre-crop-and-fill treatment as activity cards."""
    return _source_image(content, size, "#243648")


def _period_play_label(period: str) -> str:
    return {
        "yesterday": "昨日游玩",
        "week": "本周游玩",
        "month": "本月游玩",
    }.get(period, "今日游玩")


def _visible_games(
    member: TrackedMember, snapshot: LeaderboardSnapshot, personal_name: str | None,
) -> tuple[TrackedGame, ...]:
    # Filter presentation only: keep the snapshot's totals and member order intact.
    if snapshot.period == "month" and personal_name is None:
        return tuple(game for game in member.games if game.seconds > 3600)
    return member.games


def _render(
    snapshot: LeaderboardSnapshot,
    assets: dict[str, bytes | None],
    personal_name: str | None = None,
) -> bytes:
    members = snapshot.members
    width = 900
    outer = 28
    header_height = 150
    gap = 14
    visible_games = [_visible_games(member, snapshot, personal_name) for member in members]
    row_heights = [196 + (len(games) - 1) * 38 if games else 150 for games in visible_games]
    height = outer * 2 + header_height + sum(row_heights) + gap * len(members)
    canvas = Image.new("RGB", (width, max(height, 300)), "#08111a")
    glow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow)
    glow_draw.ellipse((-240, -300, 800, 620), fill=(0, 112, 205, 65))
    glow_draw.ellipse((500, 650, 1300, 1550), fill=(230, 0, 18, 25))
    glow = glow.filter(ImageFilter.GaussianBlur(110))
    canvas = Image.alpha_composite(canvas.convert("RGBA"), glow).convert("RGB")
    draw = ImageDraw.Draw(canvas)

    local_start = snapshot.day_start
    local_poll = (snapshot.last_poll or snapshot.day_start).astimezone(local_start.tzinfo)
    if snapshot.period == "month":
        title = f"{local_start:%Y年%m月}游戏时长排行榜"
        subtitle = (
            f"{local_start:%Y年%m月} · {local_start:%m月%d日 %H:%M}–"
            f"{local_poll:%m月%d日 %H:%M} · {len(members)}位玩家"
        )
    elif snapshot.period == "week":
        title = "本周游戏时长排行榜"
        subtitle = (
            f"{local_start:%Y年%m月%d日} {local_start:%H:%M}–"
            f"{local_poll:%m月%d日 %H:%M} · {len(members)}位玩家"
        )
    elif snapshot.period == "yesterday":
        title = "昨日游戏时长排行榜"
        subtitle = (
            f"{local_start:%Y年%m月%d日 %H:%M}–{local_poll:%m月%d日 %H:%M} · "
            f"{len(members)}位玩家"
        )
    else:
        title = "今日游戏时长排行榜"
        subtitle = (
            f"{local_start:%Y年%m月%d日} · {local_start:%H:%M}–{local_poll:%H:%M} · "
            f"{len(members)}位玩家"
        )
    if personal_name is not None:
        title = _period_play_label(snapshot.period).replace("游玩", "游戏时长")
        subtitle = f"{local_start:%Y年%m月%d日 %H:%M}–{local_poll:%m月%d日 %H:%M}"
        if not members:
            empty_text, empty_font = _fitted_text(draw, f"{personal_name}：暂无可统计的游戏时长", 810, 26, 18)
            draw.text((44, 195), empty_text, font=empty_font, fill="#b9cad7")
    draw.text((42, 48), title, font=_font(38, bold=True), fill="#f7fbff")
    draw.text((44, 105), subtitle, font=_font(19), fill="#91a7b9")

    top = outer + header_height + gap
    for index, (member, row_height, games) in enumerate(zip(members, row_heights, visible_games), 1):
        bottom = top + row_height
        card_left = outer
        card_right = width - outer
        rank_colour = RANK_COLOURS[index - 1] if index <= 3 else "#60788c"
        draw.rounded_rectangle(
            (card_left, top, card_right, bottom),
            radius=26,
            fill="#172738" if index == 1 else "#15222f",
            outline=rank_colour if index <= 3 else "#2b4052",
            width=2,
        )
        draw.rounded_rectangle((card_left, top, card_left + 8, bottom), radius=4, fill=rank_colour)

        rank_box = (48, top + 28, 98, top + 78)
        if personal_name is None:
            draw.ellipse(rank_box, fill=rank_colour)
            _centred(
                draw,
                rank_box,
                str(index),
                _font(23, bold=True),
                "#17202a" if index <= 3 else "#ffffff",
            )

        avatar_content = assets.get(member.avatar_url or "")
        avatar = _source_image(avatar_content, (92, 92), "#26394b")
        _paste_rounded(canvas, avatar, (118, top + 30), 46, circle=True)
        draw.ellipse((115, top + 27, 213, top + 125), outline=rank_colour, width=3)
        if not avatar_content:
            _render_fallback_mark(draw, (118, top + 30, 210, top + 122), member.name, "#d9e7f4")

        name, name_font = _fitted_text(draw, member.name, 250, 28, 19)
        draw.text((232, top + 24), name, font=name_font, fill="#f6f9fc")
        draw.text((232, top + 64), _period_play_label(snapshot.period), font=_font(15), fill="#7f96a8")

        total_text = _duration(member.total_seconds)
        total_font = _font(19, bold=True)
        total_bounds = draw.textbbox((0, 0), total_text, font=total_font)
        draw.text(
            (850 - (total_bounds[2] - total_bounds[0]), top + 23),
            total_text,
            font=total_font,
            fill="#52b8ff",
        )

        if not games:
            draw.text((232, top + 98), "暂无超过1小时的游戏", font=_font(17), fill="#b9cad7")
            top = bottom + gap
            continue
        longest = games[0]
        cover_content = assets.get(longest.image_url or "")
        cover = _leaderboard_cover(cover_content, (92, 125))
        _paste_rounded(canvas, cover, (758, top + 62), 14)
        if not cover_content:
            draw.rounded_rectangle((782, top + 103, 826, top + 132), radius=10, outline="#52b8ff", width=3)
            draw.line((791, top + 117, 802, top + 117), fill="#52b8ff", width=3)
            draw.line((796, top + 112, 796, top + 122), fill="#52b8ff", width=3)

        list_top = top + 94
        max_seconds = longest.seconds
        for game_index, game in enumerate(games):
            y = list_top + game_index * 38
            label, colour = PLATFORM.get(game.platform.casefold(), (game.platform, "#526c80"))
            badge = (232, y, 304, y + 25)
            draw.rounded_rectangle(badge, radius=13, fill=colour)
            _centred(draw, badge, label, _font(13, bold=True), "#ffffff")

            game_left = 316
            game_text, game_font = _fitted_text(draw, game.name, 258, 17, 14)
            draw.text((game_left, y + 1), game_text, font=game_font, fill="#e7eef5")
            duration_text = _duration(game.seconds)
            duration_font = _font(15, bold=True)
            duration_bounds = draw.textbbox((0, 0), duration_text, font=duration_font)
            draw.text(
                (742 - (duration_bounds[2] - duration_bounds[0]), y + 2),
                duration_text,
                font=duration_font,
                fill="#b9cad7",
            )
            bar_y = y + 29
            draw.rounded_rectangle((game_left, bar_y, 742, bar_y + 4), radius=2, fill="#263b4c")
            progress = int((742 - game_left) * game.seconds / max_seconds)
            draw.rounded_rectangle((game_left, bar_y, game_left + progress, bar_y + 4), radius=2, fill=colour)

        top = bottom + gap

    output = BytesIO()
    canvas.save(output, format="PNG", optimize=True)
    return output.getvalue()


def _prepare_image(content: bytes) -> bytes:
    """Keep the complete leaderboard in one image, preferring original dimensions."""
    if len(content) <= MAX_IMAGE_BYTES:
        return content
    with Image.open(BytesIO(content)) as source:
        original_size = source.size
        source = source.convert("RGB")
        # JPEG cannot encode arbitrarily long images.
        scale = min(1.0, 65000 / max(source.size))
        while True:
            size = tuple(max(1, int(value * scale)) for value in original_size)
            image = source if size == original_size else source.resize(size, Image.Resampling.LANCZOS)
            for quality in (90, 85, 80):
                output = BytesIO()
                image.save(output, format="JPEG", quality=quality, subsampling=0)
                encoded = output.getvalue()
                if len(encoded) <= MAX_IMAGE_BYTES:
                    logger.info(
                        "Leaderboard compressed to one image: original_size=%s output_size=%s "
                        "original_bytes=%s output_bytes=%s quality=%s",
                        original_size, size, len(content), len(encoded), quality,
                    )
                    return encoded
            if size == (1, 1):
                raise ValueError("Leaderboard image exceeds send size budget")
            scale *= 0.85

async def build_leaderboard_image(
    snapshot: LeaderboardSnapshot, *, personal_name: str | None = None,
) -> bytes:
    entries = [
        ListCardEntry(
            name=member.name,
            current_game=games[0].name if games else None,
            avatar_url=member.avatar_url,
            game_image_url=games[0].image_url if games else None,
        )
        for member in snapshot.members
        for games in [_visible_games(member, snapshot, personal_name)]
    ]
    assets = await _load_assets(entries)
    content = await asyncio.to_thread(_render, snapshot, assets, personal_name)
    return await asyncio.to_thread(_prepare_image, content)


async def build_leaderboard_message(
    snapshot: LeaderboardSnapshot, *, personal_name: str | None = None,
) -> Message:
    content = await build_leaderboard_image(snapshot, personal_name=personal_name)
    return Message(MessageSegment.image(content, cache=False, proxy=False, timeout=30))

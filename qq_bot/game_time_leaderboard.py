from __future__ import annotations

import asyncio
import logging
from io import BytesIO

from nonebot.adapters.onebot.v11 import Message, MessageSegment
from PIL import Image, ImageDraw

from .game_time_tracker import LeaderboardSnapshot, TrackedGame, TrackedMember
from .list_cards import (
    ListCardEntry,
    _load_assets,
    _source_image,
)


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
    from .rank_board_card import render_rank_board
    return render_rank_board(snapshot, assets, personal_name)


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
            current_game=game.name if game else None,
            avatar_url=member.avatar_url,
            game_image_url=game.image_url if game else None,
        )
        for member in snapshot.members
        for games in [_visible_games(member, snapshot, personal_name)]
        for game in (games or (None,))
    ]
    assets = await _load_assets(entries)
    content = await asyncio.to_thread(_render, snapshot, assets, personal_name)
    return await asyncio.to_thread(_prepare_image, content)


async def build_leaderboard_message(
    snapshot: LeaderboardSnapshot, *, personal_name: str | None = None,
) -> Message:
    content = await build_leaderboard_image(snapshot, personal_name=personal_name)
    return Message(MessageSegment.image(content, cache=False, proxy=False, timeout=30))

"""Load the personal dashboard with a four-minute image deadline."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import replace

from nonebot.adapters.onebot.v11 import Message, MessageSegment

from .game_time_leaderboard import _prepare_image
from .personal_rank_assets import load_personal_rank_assets
from .personal_rank_card import MAX_GAME_TILES, _decoded_asset, render_personal_rank, timeout_cover, requires_landscape
from .personal_rank_data import build_personal_rank_data, personal_rank_month_end
from .personal_rank_psn import enrich_personal_rank_psn

logger = logging.getLogger(__name__)
IMAGE_RETRY_INITIAL_SECONDS = 5
IMAGE_RETRY_MAX_SECONDS = 60
IMAGE_WAIT_TIMEOUT_SECONDS = 240


class ImagesNotReady(ValueError):
    """Internal pending state; never sent to the group."""


def _require_loaded_images(data, assets) -> None:
    """Do not send a partially populated dashboard while required art is missing."""
    games = list(data.games[:MAX_GAME_TILES])
    if data.recent_games:
        games.append(max(data.recent_games, key=lambda game: game.seconds))
    if any(_decoded_asset(assets.get(game.image_url), landscape=requires_landscape(game)) is None for game in games):
        raise ImagesNotReady("横版封面暂未准备齐全")
    if data.avatar_url and _decoded_asset(assets.get(data.avatar_url)) is None:
        raise ImagesNotReady("头像暂未加载完成")


def _with_timeout_covers(data, assets):
    """Insert temporary placeholders without writing them to the cover cache."""
    assets = dict(assets)
    placeholder = timeout_cover()
    def fill(games):
        result = []
        for index, game in enumerate(games):
            if _decoded_asset(assets.get(game.image_url), landscape=requires_landscape(game)) is None:
                key = f"rank-timeout://{game.platform}/{game.name}/{index}"
                assets[key] = placeholder
                game = replace(game, image_url=key)
            result.append(game)
        return tuple(result)
    return replace(data, games=fill(data.games), recent_games=fill(data.recent_games)), assets


async def build_personal_rank_message(tracker, group_id: str, member_name: str,
                                     *, month: int | None = None) -> Message:
    # Keep the historical reference fixed even when cover loading retries.
    options = {} if month is None else {
        "now": personal_rank_month_end(month), "finalized_month": True,
    }
    deadline = asyncio.get_running_loop().time() + IMAGE_WAIT_TIMEOUT_SECONDS
    data = await asyncio.to_thread(build_personal_rank_data, tracker, group_id, member_name, **options)
    assets = {}
    enriched_data = None
    delay = IMAGE_RETRY_INITIAL_SECONDS

    async def load_images(snapshot):
        nonlocal data
        loaded, new_assets = await load_personal_rank_assets(snapshot)
        assets.update({key: value for key, value in new_assets.items() if value})
        data = loaded

    try:
        async with asyncio.timeout_at(deadline) as wait_limit:
            snapshot = data
            while True:
                if enriched_data is None:
                    _, enriched_data = await asyncio.gather(
                        load_images(snapshot), enrich_personal_rank_psn(snapshot, group_id, tracker=tracker),
                    )
                else:
                    await load_images(snapshot)
                data = replace(data, platinum_count=enriched_data.platinum_count,
                               achievements={**data.achievements, **{
                                   key: value for key, value in enriched_data.achievements.items() if value == "白金"
                               }})
                try:
                    await asyncio.to_thread(_require_loaded_images, data, assets)
                    break
                except ImagesNotReady:
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, IMAGE_RETRY_MAX_SECONDS)
                    snapshot = await asyncio.to_thread(
                        build_personal_rank_data, tracker, group_id, member_name, **options,
                    )
    except TimeoutError:
        if not wait_limit.expired():
            raise
        logger.info("Personal rank image wait expired for group %s; sending placeholders", group_id)
        data, assets = await asyncio.to_thread(_with_timeout_covers, data, assets)
    content = await asyncio.to_thread(render_personal_rank, data, assets)
    content = await asyncio.to_thread(_prepare_image, content)
    return Message(MessageSegment.image(content, cache=False, proxy=False, timeout=30))

"""Group game popularity, counted by distinct tracked member rather than hours."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from nonebot.adapters.onebot.v11 import Message, MessageSegment

from .game_time_tracker import TrackedGame, _identity_key
from .hot_game_identity import title_identities, title_key

logger = logging.getLogger(__name__)
MAX_HOT_GAMES = 6


@dataclass(frozen=True)
class HotGame:
    platform: str
    name: str
    seconds: float
    image_url: str | None
    players: int


@dataclass(frozen=True)
class HotGames:
    period: str
    games: tuple[HotGame, ...]


def aggregate_hot_games(snapshot, identities=None) -> HotGames:
    identities = title_identities() if identities is None else identities
    games: dict[str, TrackedGame] = {}
    members: dict[str, set[str]] = {}
    for member in snapshot.members:
        for game in member.games:
            if game.seconds <= 0 or not game.name.strip():
                continue
            name = identities.get(title_key(game.name), game.name)
            key = title_key(name)
            members.setdefault(key, set()).add(_identity_key(member.name))
            if key not in games or (not games[key].image_url and game.image_url):
                games[key] = replace(game, name=name)
    rows = [HotGame(g.platform, g.name, 0, g.image_url, len(members[key]))
            for key, g in games.items()]
    rows.sort(key=lambda g: (-g.players, _identity_key(g.name)))
    if len(rows) > MAX_HOT_GAMES:
        cutoff = rows[MAX_HOT_GAMES - 1].players
        rows = [game for game in rows if game.players >= cutoff]
    return HotGames(snapshot.period, tuple(rows))


def hot_game_ranks(games) -> tuple[int, ...]:
    """Competition ranks: 1, 1, 3; equal counts share a medal."""
    ranks = []
    for index, game in enumerate(games):
        ranks.append(ranks[-1] if index and game.players == games[index - 1].players else index + 1)
    return tuple(ranks)


def build_hot_games_data(tracker, group_id: str, period: str = "week", *, at=None) -> HotGames:
    if period not in {"week", "month"}:
        raise ValueError("未知热游周期")
    # Use wall time even if the last platform poll belongs to an older week.
    reference = at or datetime.now(timezone.utc)
    snapshot = (tracker.weekly_snapshot if period == "week" else tracker.monthly_snapshot)(
        str(group_id), at=reference, board="all",
    )
    return aggregate_hot_games(snapshot, title_identities(getattr(tracker, "path", None), str(group_id)))


async def build_hot_games_message(tracker, group_id: str, period: str = "week") -> Message:
    from .game_time_leaderboard import _prepare_image
    from .personal_rank_assets import load_personal_rank_assets
    from .hot_games_card import render_hot_games

    data = await asyncio.to_thread(build_hot_games_data, tracker, group_id, period)
    assets = {}
    loaded_games = []
    # The shared cover loader handles six visible games per batch.
    for start in range(0, len(data.games), MAX_HOT_GAMES):
        batch = replace(data, games=data.games[start:start + MAX_HOT_GAMES])
        try:
            batch, batch_assets = await load_personal_rank_assets(batch)
            assets.update(batch_assets)
        except Exception:
            logger.exception("Hot game covers unavailable for group %s", group_id)
        loaded_games.extend(batch.games)
    data = replace(data, games=tuple(loaded_games))
    content = await asyncio.to_thread(render_hot_games, data, assets)
    content = await asyncio.to_thread(_prepare_image, content)
    return Message(MessageSegment.image(content, cache=False, proxy=False, timeout=30))

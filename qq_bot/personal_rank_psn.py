"""Read personal platinum totals from the last complete local trophy sync."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import logging

from .game_time_tracker import month_start
from .ps_trophy_store import read_platinum_summary, trophy_db_path

logger = logging.getLogger(__name__)


def _key(value: str) -> str:
    return ' '.join(value.split()).casefold()


async def enrich_personal_rank_psn(data, group_id: str, *, tracker=None):
    try:
        if tracker is not None:
            from .personal_rank_data import read_bound_account_ids
            accounts = tuple(sorted(await asyncio.to_thread(
                read_bound_account_ids, tracker, group_id, data.name, 'ps')))
        else:
            from .ps5_registry import registry
            items = await asyncio.to_thread(registry.list_group, group_id)
            accounts = tuple(sorted({item.account_id for item in items
                                     if _key(item.nickname or item.online_id) == _key(data.name)}))
        if not accounts:
            has_ps = any(game.platform.casefold() in {'ps', 'ps5'} for game in data.games)
            return replace(data, platinum_count=None if has_ps else 0)
        result = await asyncio.to_thread(read_platinum_summary, trophy_db_path(), accounts,
                                        month_start(data.as_of), data.as_of)
        if result is None:
            return data
        count, completed = result
        badges = dict(data.achievements)
        for game in (*data.games, *data.recent_games):
            if game.platform.casefold() in {'ps', 'ps5'} and _key(game.name) in completed:
                badges[(game.platform.casefold(), game.name)] = '白金'
        return replace(data, platinum_count=count, achievements=badges)
    except Exception:
        logger.info('Local PS trophy summary unavailable', exc_info=True)
        return data

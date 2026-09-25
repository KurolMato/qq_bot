"""Persistent monthly rank images, refreshed once per gaming day (04:00 UTC+8)."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path

from nonebot.adapters.onebot.v11 import Message, MessageSegment

from .config import BotConfig
from .game_time_leaderboard import build_leaderboard_image
from .game_time_tracker import LOCAL_TZ, day_start, month_start, tracker
from .resilience import supervise, wait_for_stop

logger = logging.getLogger(__name__)
CACHE_ROOT = Path(__file__).resolve().parent.parent / 'data' / 'monthly_rank_cache'


class MonthlyRankCache:
    def __init__(self, source, root: Path = CACHE_ROOT):
        self.source = source
        self.root = root
        self.locks: dict[Path, asyncio.Lock] = {}

    @staticmethod
    def _read(directory, stamp):
        for suffix in ('.png', '.jpg', '.empty'):
            path = directory / (stamp + suffix)
            if path.is_file():
                return path.read_bytes()
        return None

    @staticmethod
    def _write(directory, stamp, content):
        directory.mkdir(parents=True, exist_ok=True)
        suffix = '.empty' if not content else '.png' if content.startswith(b'\x89PNG') else '.jpg'
        path = directory / (stamp + suffix)
        temporary = path.with_suffix('.tmp')
        temporary.write_bytes(content)
        temporary.replace(path)

    @staticmethod
    def _previous(directory, stamp):
        if directory.exists():
            files = sorted((p for p in directory.iterdir()
                            if p.suffix in {'.png', '.jpg', '.empty'} and p.stem < stamp), reverse=True)
            if files:
                return files[0].read_bytes()
        return None

    async def message(self, group_id: str, *, month=None, board='main', at=None):
        if not str(group_id).isdigit() or board not in {'main', 'other'}:
            raise ValueError('Invalid monthly rank cache key')
        now = at or datetime.now(LOCAL_TZ)
        current = month_start(now)
        selected = current.month if month is None else month
        if not 1 <= selected <= 12:
            raise ValueError('Invalid month')
        year = current.year if selected <= current.month else current.year - 1
        directory = self.root / str(group_id) / f'{year:04d}-{selected:02d}' / board
        stamp = day_start(now).date().isoformat()
        async with self.locks.setdefault(directory, asyncio.Lock()):
            content = await asyncio.to_thread(self._read, directory, stamp)
            if content is None:
                try:
                    snapshot = await asyncio.to_thread(
                        self.source.monthly_snapshot, str(group_id), at=now, month=month, board=board,
                    )
                    content = await build_leaderboard_image(snapshot) if snapshot.members else b''
                    await asyncio.to_thread(self._write, directory, stamp, content)
                    logger.info('Monthly rank cache updated: group=%s month=%s-%02d board=%s bytes=%s',
                                group_id, year, selected, board, len(content))
                except Exception:
                    logger.exception('Monthly rank cache update failed: group=%s board=%s', group_id, board)
                    content = await asyncio.to_thread(self._previous, directory, stamp)
                    if content is None:
                        raise
        if not content:
            return Message(f'{year:04d}年{selected:02d}月还没有可统计的游戏时长。')
        return Message(MessageSegment.image(content, cache=False, proxy=False, timeout=30))

    async def refresh(self, groups):
        for group_id in groups:
            for board in ('main', 'other'):
                try:
                    await self.message(str(group_id), board=board)
                except Exception:
                    # One group's missing cache must not stop updates for the others.
                    logger.warning('Monthly rank cache unavailable: group=%s board=%s', group_id, board)


monthly_cache = MonthlyRankCache(tracker)
_task = None
_stop = None


async def start_monthly_rank_cache():
    global _task, _stop
    _stop = asyncio.Event()
    allowed = BotConfig.from_env().allowed_groups

    async def run():
        while not _stop.is_set():
            groups = allowed or await asyncio.to_thread(tracker.group_ids)
            await monthly_cache.refresh(groups)
            await wait_for_stop(_stop, 60)

    _task = asyncio.create_task(supervise('monthly-rank-cache', run, _stop.is_set))


async def stop_monthly_rank_cache():
    if _stop:
        _stop.set()
    if _task:
        _task.cancel()
        await asyncio.gather(_task, return_exceptions=True)

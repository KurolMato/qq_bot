"""Twice-daily PSN trophy polling, independent of presence and QQ delivery."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta

from .config import BotConfig
from .game_time_tracker import LOCAL_TZ
from .ps_trophy_store import KINDS, TrophyStore, trophy_db_path
from .resilience import supervise, wait_for_stop

logger = logging.getLogger(__name__)
PAGE_SIZE = 100
DETAIL_BATCH_SIZE = 5
MAX_TROPHIES_PER_TITLE = 2000
CHECK_TIMES = ((4, 10), (16, 10))


class TrophyChanged(ValueError):
    """A title changed mid-scan; rediscover it before accepting details."""


def next_check(now: float) -> float:
    local = datetime.fromtimestamp(now, LOCAL_TZ)
    for days in (0, 1):
        for hour, minute in CHECK_TIMES:
            candidate = (local + timedelta(days=days)).replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate > local:
                return candidate.timestamp()
    raise AssertionError('Missing next trophy check')


def counts(value) -> tuple[int, ...]:
    result = tuple(getattr(value, kind, None) for kind in KINDS)
    if any(type(number) is not int or number < 0 for number in result):
        raise ValueError('Invalid PSN trophy counts')
    return result


def parse_title(title) -> dict:
    if title.hidden_flag:
        raise ValueError('Hidden trophy title prevents a complete baseline')
    service = title.np_service_name
    if service not in {'trophy', 'trophy2'} or not title.np_communication_id:
        raise ValueError('Trophy title identity unavailable')
    platforms = [p for p in title.title_platform if p.value != 'UNKNOWN' and p.get_trophy_service_name() == service]
    if not platforms:
        raise ValueError('Trophy platform unavailable')
    platform = sorted(platforms, key=lambda p: p.value)[0]
    updated = title.last_updated_datetime
    if updated is not None and updated.tzinfo is None:
        raise ValueError('Trophy update time lacks timezone')
    return dict(service=service, title_id=title.np_communication_id, platform=platform.value,
                name=title.title_name, icon_url=title.title_icon_url,
                counts=counts(title.earned_trophies), updated=updated.timestamp() if updated else None)


def parse_trophies(values, expected: tuple[int, ...]) -> list[dict]:
    result, seen = [], set()
    actual = [0] * len(KINDS)
    for index, trophy in enumerate(values):
        if index >= MAX_TROPHIES_PER_TITLE:
            raise ValueError('Trophy title exceeded bounded scan')
        if trophy.earned is not True:
            continue
        kind = getattr(trophy.trophy_type, 'value', None)
        earned = trophy.earned_date_time
        identity = trophy.trophy_id
        if (kind not in KINDS or identity is None or earned is None or earned.tzinfo is None
                or earned.timestamp() <= 0 or earned.timestamp() > time.time() + 300):
            raise ValueError('Earned trophy identity or unlock time unavailable')
        if str(identity) in seen:
            raise ValueError('Duplicate trophy in detail response')
        seen.add(str(identity))
        actual[KINDS.index(kind)] += 1
        result.append(dict(id=str(identity), kind=kind, name=trophy.trophy_name, earned_at=earned.timestamp()))
    if tuple(actual) != expected:
        raise TrophyChanged('Trophy details disagree with title counts')
    return result


class TrophyMonitor:
    def __init__(self, registry, client, path=None):
        self.registry, self.client = registry, client
        self.store = TrophyStore(path if path is not None else trophy_db_path())
        self.allowed = BotConfig.from_env().allowed_groups
        self.stop = asyncio.Event()

    async def summary(self, account):
        return await self.client._call('trophy count check', lambda: counts(
            self.client._get_api().user(account_id=account).trophy_summary().earned_trophies))

    async def step(self, account: str):
        now = time.time()
        state = await asyncio.to_thread(self.store.state, account)
        if state['retry_at'] > now or (state['stage'] == 'idle' and state['due'] > now):
            return
        try:
            if state['stage'] == 'idle':
                current = await self.summary(account)
                previous = tuple(json.loads(state['counts'])) if state['counts'] is not None else None
                if previous is not None and any(new < old for new, old in zip(current, previous)):
                    raise ValueError('Account trophy counts decreased; keeping saved baseline')
                if current == previous:
                    await asyncio.to_thread(self.store.unchanged, account, now, next_check(time.time()))
                    return
                await asyncio.to_thread(self.store.begin, account, current, now)
                logger.info('PS trophy sync started: account=%s counts=%s', account, current)
                return
            if state['stage'] == 'listing':
                def page():
                    user = self.client._get_api().user(account_id=account)
                    # PSNAWP subtracts the starting offset from limit, so use
                    # the absolute end of this batch to keep HTTP limit positive.
                    return [parse_title(title) for title in user.trophy_titles(
                        limit=state['offset'] + PAGE_SIZE, offset=state['offset'], page_size=PAGE_SIZE)]
                values = await self.client._call('trophy game discovery', page)
                await asyncio.to_thread(self.store.save_page, account, values,
                                       offset=state['offset'] + len(values), finished=len(values) < PAGE_SIZE)
                return
            pending = await asyncio.to_thread(self.store.pending, account, DETAIL_BATCH_SIZE)
            for title in pending:
                def details():
                    from psnawp_api.models.trophies import PlatformType
                    user = self.client._get_api().user(account_id=account)
                    # All DLC groups contribute to the account trophy counts.
                    values = user.trophies(title['title_id'], PlatformType(title['platform']),
                        include_progress=True, trophy_group_id='all', limit=MAX_TROPHIES_PER_TITLE + 1)
                    return parse_trophies(values, tuple(json.loads(title['counts'])))
                # Ps5Client wraps exceptions. Preserve known sample-validation
                # errors so a changing response can restart discovery explicitly.
                def checked_details():
                    try:
                        return details()
                    except ValueError as exc:
                        return exc
                result = await self.client._call('trophy unlock sync', checked_details)
                if isinstance(result, ValueError):
                    raise result
                await asyncio.to_thread(self.store.save_trophies, title, result, time.time())
            if await asyncio.to_thread(self.store.pending, account, 1):
                return
            latest = await self.summary(account)
            if latest != tuple(json.loads(state['target'])):
                raise TrophyChanged('Account changed during trophy sync')
            finished = time.time()
            try:
                await asyncio.to_thread(self.store.finish, account, finished, next_check(finished))
            except ValueError as exc:
                raise TrophyChanged(str(exc)) from exc
            logger.info('PS trophy sync complete: account=%s counts=%s', account, latest)
        except Exception as exc:
            delay = await asyncio.to_thread(self.store.failed, account, time.time(), type(exc).__name__,
                                   restart=isinstance(exc, TrophyChanged), invalid_data=isinstance(exc, ValueError))
            # Validation messages are generated locally; wrapped HTTP errors
            # may contain credentials, so only expose their type.
            reason = str(exc)[:300] if isinstance(exc, ValueError) else type(exc).__name__
            logger.warning('PS trophy sync deferred: account=%s reason=%s detail=%s; retry in %ds',
                           account, type(exc).__name__, reason, delay)

    async def poll_once(self):
        if not self.client.configured:
            return
        items = await asyncio.to_thread(self.registry.list_all)
        accounts = sorted({item.account_id for item in items
                           if not self.allowed or item.group_id in self.allowed})
        for account in accounts:
            if self.stop.is_set():
                return
            await self.step(account)

    async def run(self):
        while not self.stop.is_set():
            await self.poll_once()
            await wait_for_stop(self.stop, 60)


_monitor = None
_task = None


async def start_trophy_monitor():
    from .ps5_registry import Ps5Client, registry
    global _monitor, _task
    _monitor = TrophyMonitor(registry, Ps5Client())
    _task = asyncio.create_task(supervise('ps-trophy-monitor', _monitor.run, _monitor.stop.is_set))
    logger.info('PS trophy monitor ready: daily 04:10/16:10 UTC+8')


async def stop_trophy_monitor():
    global _monitor, _task
    if _monitor:
        _monitor.stop.set()
    if _task:
        _task.cancel()
        await asyncio.gather(_task, return_exceptions=True)
    if _monitor:
        _monitor.client._executor.shutdown(wait=False, cancel_futures=True)
    _monitor = None
    _task = None

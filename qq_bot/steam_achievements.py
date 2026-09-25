"""Independent, bounded achievement polling. No work is awaited by presence polling."""
import asyncio
import json
import logging
import time
from pathlib import Path

import nonebot
from nonebot.adapters.onebot.v11 import Bot, ActionFailed

from .config import BotConfig
from .steam_achievement_store import AchievementStore
from .steam_achievement_card import build_message
from .resilience import wait_for_stop, supervise

logger = logging.getLogger(__name__)


class AchievementUnavailable(ValueError):
    """The account or game exposes no usable achievements; not an outage."""


class AchievementRetrySoon(RuntimeError):
    """Steam has a schema but the player's newly synced progress is not ready yet."""


def parse_progress(payload):
    player = payload.get('playerstats', {})
    values = player.get('achievements')
    if player.get('success') is not True or not isinstance(values, list) or not values:
        raise AchievementRetrySoon('Player achievement data is temporarily incomplete')
    result = {}
    for value in values:
        name, achieved = value.get('apiname'), value.get('achieved')
        if not isinstance(name, str) or not name or achieved not in (0, 1) or name in result:
            raise ValueError('Invalid achievement response')
        result[name] = (achieved == 1, value.get('unlocktime', 0))
    return result


def achievement_sync_http_error(error):
    current = error
    while current:
        response = getattr(current, 'response', None)
        if (getattr(response, 'status_code', None) in {400, 403} or
                getattr(current, 'steam_status', None) in {400, 403}):
            return True
        current = current.__cause__
    return False


def _playtime_minutes(data, app):
    response = data.get('response') if isinstance(data, dict) else None
    games = response.get('games') if isinstance(response, dict) else None
    if not isinstance(games, list):
        return None
    for game in games:
        if isinstance(game, dict) and str(game.get('appid')) == str(app):
            minutes = game.get('playtime_forever')
            if type(minutes) is int and minutes >= 0:
                return minutes
    return None


class AchievementMonitor:
    def __init__(self, registry, client, path=None):
        self.registry, self.client = registry, client
        self.store = AchievementStore(path or Path(__file__).resolve().parent.parent / 'data' / 'steam-achievements.db')
        self.allowed = BotConfig.from_env().allowed_groups
        self.current = {}
        self._stop = asyncio.Event()
        self.timeout = 30

    def observe_players(self, players):
        now = time.time()
        for sid, player in players.items():
            app = str(player.get('gameid', ''))
            if app.isdigit():
                self.current[sid] = (app, str(player.get('gameextrainfo') or app), now)
            else:
                self.current.pop(sid, None)

    async def subscriptions(self):
        return [s for s in await asyncio.to_thread(self.registry.list_all)
                if not self.allowed or s.group_id in self.allowed]

    async def schema(self, app, force=False):
        now = time.time()
        names = None if force else await asyncio.to_thread(self.store.schema, app, now)
        if names is None:
            data = await self.client._get('ISteamUserStats/GetSchemaForGame/v2/', appid=app, l='english')
            values = data.get('game', {}).get('availableGameStats', {}).get('achievements')
            if not isinstance(values, list) or not values:
                raise AchievementUnavailable('Game has no published achievement schema')
            names = {v['name'] for v in values if isinstance(v.get('name'), str) and v['name']}
            if len(names) != len(values):
                raise ValueError('Incomplete achievement schema')
            await asyncio.to_thread(self.store.save_schema, app, names, now)
        return names

    async def check(self, sid, app, title):
        # Unsupported titles often make GetPlayerAchievements answer 400/500.
        # Verify that a real, non-empty schema exists before querying a player.
        schema = await self.schema(app)
        try:
            raw = await self.client._get('ISteamUserStats/GetPlayerAchievements/v1/', steamid=sid,
                                         appid=app, l='english', _quiet_statuses={400, 403})
        except Exception as exc:
            if achievement_sync_http_error(exc):
                raise AchievementRetrySoon('Player achievements are not ready; retry shortly') from None
            raise
        progress = parse_progress(raw)
        complete = all(v[0] for v in progress.values())
        if complete:
            schema = await self.schema(app, force=True)
        if set(progress) != schema:
            raise ValueError('Player achievements and current schema disagree; ignoring sample')
        now = time.time()
        unlocks = [v[1] for v in progress.values()]
        valid_times = all(isinstance(t, (int, float)) and 0 < t <= now for t in unlocks)
        payload = dict(title=title, total=len(schema), achieved_at=max(unlocks) if complete and valid_times else None,
                       detected_at=now)
        items = await self.subscriptions()
        groups = {s.group_id for s in items if s.steam_id == sid}
        if groups:
            created = await asyncio.to_thread(self.store.observe, sid, app, complete, payload, groups, now)
            if created:
                logger.info('Steam full achievement detected: steamid=%s appid=%s total=%s', sid, app, len(schema))

    async def discover(self, sid):
        data = await self.client._get('IPlayerService/GetRecentlyPlayedGames/v1/', steamid=sid)
        response = data.get('response')
        if not isinstance(response, dict) or 'total_count' not in response:
            raise AchievementUnavailable('Recent games are private or unavailable')
        games = response.get('games', [])
        if not isinstance(games, list):
            raise ValueError('Invalid recent games')
        for game in games:
            app = str(game.get('appid', ''))
            if app.isdigit():
                await asyncio.to_thread(self.store.target, sid, app, str(game.get('name') or app), time.time())

    async def enrich(self, sid, app, payload):
        result = dict(payload)
        # Optional metadata must not prevent the completion notification.
        for key, operation in [('title', lambda: self.client.game_name(app)),
                               ('cover', lambda: self.client.game_cover(app))]:
            try:
                value = await asyncio.wait_for(operation(), 6)
                if value:
                    result[key] = value
            except Exception:
                logger.warning('Achievement %s lookup failed for %s', key, app, exc_info=True)
        # Service interfaces require structured input_json, especially array filters.
        # Recent games can expose playtime for titles absent from the owned list.
        for endpoint, params in (
            ('GetOwnedGames', dict(steamid=sid, appids_filter=[int(app)], include_played_free_games=True)),
            ('GetRecentlyPlayedGames', dict(steamid=sid, count=0)),
        ):
            try:
                data = await asyncio.wait_for(self.client._get(
                    f'IPlayerService/{endpoint}/v1/', input_json=json.dumps(params)), 6)
                minutes = _playtime_minutes(data, app)
            except Exception as exc:
                # Do not log exception URLs: they may contain the Steam API key.
                logger.warning('Achievement playtime lookup failed: %s/%s source=%s error=%s',
                               sid, app, endpoint, type(exc).__name__)
                continue
            if minutes is not None:
                result['minutes'] = minutes
                logger.info('Achievement playtime resolved: %s/%s source=%s minutes=%s',
                            sid, app, endpoint, minutes)
                break
            logger.info('Achievement playtime missing: %s/%s source=%s; no matching lifetime minutes',
                        sid, app, endpoint)
        return result

    async def deliver(self):
        items = await self.subscriptions()
        active = {(s.steam_id, s.group_id): s for s in items}
        await asyncio.to_thread(self.store.cancel_removed, set(active))
        bots = [b for b in nonebot.get_bots().values() if isinstance(b, Bot)]
        if not bots:
            return
        metadata = {}
        for row in await asyncio.to_thread(self.store.pending, time.time()):
            sid, app, gid = row['sid'], row['app'], row['gid']
            item = active.get((sid, gid))
            if item is None:
                continue
            if (sid, app) not in metadata:
                metadata[sid, app] = await self.enrich(sid, app, row['payload'])
            message = await build_message(item.nickname or item.display_name, metadata[sid, app], item.avatar_url)
            # Recheck membership after slow image/metadata I/O.
            if not any(s.steam_id == sid and s.group_id == gid for s in await self.subscriptions()):
                await asyncio.to_thread(self.store.finish, sid, app, gid, 'cancelled', time.time())
                continue
            if not await asyncio.to_thread(self.store.claim, sid, app, gid):
                continue
            try:
                await asyncio.wait_for(bots[0].send_group_msg(group_id=int(gid), message=message), 15)
            except ActionFailed:
                state = 'pending' if row['attempts'] + 1 < 3 else 'failed'
                logger.warning('Steam achievement send rejected: %s/%s group=%s', sid, app, gid, exc_info=True)
            except asyncio.CancelledError:
                raise  # Claimed record remains uncertain on cancellation/restart.
            except Exception:
                state = 'uncertain'
                logger.warning('Steam achievement delivery uncertain; no automatic resend: %s/%s group=%s', sid, app, gid, exc_info=True)
            else:
                state = 'sent'
                logger.info('Sent Steam achievement: %s/%s group=%s', sid, app, gid)
            await asyncio.to_thread(self.store.finish, sid, app, gid, state, time.time())

    async def poll_once(self):
        if not self.client.configured:
            return
        now = time.time()
        items = await self.subscriptions()
        users = {s.steam_id for s in items}
        self.current = {sid: v for sid, v in self.current.items() if sid in users and now-v[2] < 600}
        for sid, (app, title, _) in self.current.items():
            await asyncio.to_thread(self.store.target, sid, app, title, now)
            await asyncio.to_thread(self.store.expedite, f'check:{sid}:{app}', now)
        jobs = []
        for target in await asyncio.to_thread(self.store.targets, now):
            sid, app, title = target['sid'], target['app'], target['title']
            key = f'check:{sid}:{app}'
            if sid in users and await asyncio.to_thread(self.store.due, key, now):
                interval = 300 if self.current.get(sid, ('',))[0] == app else 1800
                jobs.append((key, interval, self.check, (sid, app, title)))
        for sid in sorted(users):
            key = 'discover:' + sid
            if await asyncio.to_thread(self.store.due, key, now):
                jobs.append((key, 1800, self.discover, (sid,)))

        async def worker():
            while jobs and not self._stop.is_set():
                key, interval, operation, args = jobs.pop(0)
                failed = False
                try:
                    async with asyncio.timeout(self.timeout):
                        await operation(*args)
                except AchievementUnavailable as exc:
                    await asyncio.to_thread(self.store.suppress, key, time.time())
                    logger.info('Steam achievement data skipped: %s (%s); retry in 7d', key, exc)
                    continue
                except AchievementRetrySoon as exc:
                    # A freshly unlocked final achievement can briefly leave the
                    # schema and player progress out of sync. Do not suppress it
                    # as a privacy/unsupported result or the completion can be lost.
                    await asyncio.to_thread(self.store.schedule, key, time.time(), 600, False)
                    logger.info('Steam achievement data pending sync: %s (%s); retry in 10m', key, exc)
                    continue
                except Exception as exc:
                    failed = True
                    # HTTP exception tracebacks may contain an API key URL.
                    logger.warning('Steam achievement query failed: %s (%s); applying backoff',
                                   key, type(exc).__name__)
                await asyncio.to_thread(self.store.schedule, key, time.time(), interval, failed)
        workers = [asyncio.create_task(worker()) for _ in range(2)]
        try:
            await asyncio.gather(*workers)
        finally:
            for task in workers:
                task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
        await self.deliver()

    async def run(self):
        while not self._stop.is_set():
            await self.poll_once()
            await wait_for_stop(self._stop, 15)


_monitor = None
_task = None


def observe_players(players):
    if _monitor:
        _monitor.observe_players(players)


async def start_achievement_monitor():
    global _monitor, _task
    from .steam_registry import registry, SteamClient
    _monitor = AchievementMonitor(registry, SteamClient(registry))
    _task = asyncio.create_task(supervise('steam-achievements', _monitor.run, _monitor._stop.is_set))
    logger.info('Steam achievement monitor ready (configured=%s)', _monitor.client.configured)


async def stop_achievement_monitor():
    global _monitor, _task
    if _monitor:
        _monitor._stop.set()
    if _task:
        _task.cancel()
        await asyncio.gather(_task, return_exceptions=True)
    _monitor = _task = None

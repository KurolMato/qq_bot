import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from qq_bot.steam_achievement_store import AchievementStore
from qq_bot.steam_achievements import (
    AchievementMonitor, AchievementRetrySoon, AchievementUnavailable, parse_progress,
)


def test_baseline_transition_restart_groups_and_dlc(tmp_path):
    path = tmp_path / 'a.db'
    store = AchievementStore(path)
    payload = {'title': 'Game'}
    assert not store.observe('s', 'old', True, payload, ['1'], 1)
    store.observe('s', 'old', False, payload, ['1'], 2)
    assert not store.observe('s', 'old', True, payload, ['1'], 3)
    assert not store.observe('s', 'new', False, payload, ['1', '2'], 1)
    assert store.observe('s', 'new', True, payload, ['1', '2'], 2)
    store = AchievementStore(path)
    assert not store.observe('s', 'new', True, payload, ['1', '2', '3'], 3)
    pending = store.pending(4)
    assert {r['gid'] for r in pending} == {'1', '2'}
    assert store.claim('s', 'new', '1')
    assert not store.claim('s', 'new', '1')
    store.finish('s', 'new', '1', 'sent', 5)
    store.cancel_removed(set())
    assert not store.pending(100)


def test_uncertain_delivery_not_replayed_after_restart(tmp_path):
    store = AchievementStore(tmp_path / 'a.db')
    store.observe('s', 'a', False, {}, ['g'], 1)
    store.observe('s', 'a', True, {}, ['g'], 2)
    store.claim('s', 'a', 'g')
    assert not AchievementStore(store.path).pending(999)


@pytest.mark.parametrize('data', [{}, {'playerstats': {'success': False}},
    {'playerstats': {'success': True, 'achievements': []}}])
def test_invalid_achievement_response(data):
    with pytest.raises(AchievementRetrySoon):
        parse_progress(data)


def test_malformed_achievement_response_is_transient_failure():
    data = {'playerstats': {'success': True, 'achievements': [{'apiname': 'a', 'achieved': 7}]}}
    with pytest.raises(ValueError):
        parse_progress(data)


def test_unavailable_game_is_suppressed_for_seven_days(tmp_path):
    async def run():
        m, _ = make_monitor(tmp_path)
        async def unavailable(*args):
            raise AchievementUnavailable('unsupported')
        m.check = unavailable
        m.store.target('s', 'a', 'Game', time.time())
        async def noop():
            pass
        m.deliver = noop
        await m.poll_once()
        with m.store.db() as db:
            row = db.execute("SELECT due,failures FROM schedules WHERE key='check:s:a'").fetchone()
        assert row['failures'] == -1 and row['due'] > time.time() + 6 * 86400
        m.observe_players({'s': {'gameid': 'a', 'gameextrainfo': 'Game'}})
        await m.poll_once()
        assert not m.store.due('check:s:a', time.time())
    asyncio.run(run())


@pytest.mark.parametrize('status', [400, 403])
def test_player_achievement_status_retries_soon(tmp_path, status):
    async def run():
        m, _ = make_monitor(tmp_path)
        async def bad(path, **kwargs):
            if 'GetSchemaForGame' in path:
                return {'game': {'availableGameStats': {'achievements': [{'name': 'a'}]}}}
            error = RuntimeError('safe')
            error.steam_status = status
            raise error
        m.client._get = bad
        with pytest.raises(AchievementRetrySoon):
            await m.check('s', 'a', 'Game')
    asyncio.run(run())


def test_sync_pending_is_retried_in_ten_minutes_not_suppressed(tmp_path):
    async def run():
        m, _ = make_monitor(tmp_path)
        async def pending(*args):
            raise AchievementRetrySoon('syncing')
        m.check = pending
        m.store.target('s', 'a', 'Game', time.time())
        async def noop():
            pass
        m.deliver = noop
        before = time.time()
        await m.poll_once()
        with m.store.db() as db:
            row = db.execute("SELECT due,failures FROM schedules WHERE key='check:s:a'").fetchone()
        assert row['failures'] == 0
        assert before + 570 < row['due'] < before + 630
    asyncio.run(run())


def test_private_recent_games_are_suppressed(tmp_path):
    async def run():
        m, _ = make_monitor(tmp_path)
        async def private(*args, **kwargs):
            return {'response': {}}
        m.client._get = private
        with pytest.raises(AchievementUnavailable):
            await m.discover('s')
    asyncio.run(run())


def test_schema_is_checked_before_player_achievements(tmp_path):
    async def run():
        m, _ = make_monitor(tmp_path)
        calls = []
        async def api(path, **kwargs):
            calls.append(path)
            return {'game': {'availableGameStats': {}}}
        m.client._get = api
        with pytest.raises(AchievementUnavailable):
            await m.check('s', 'unsupported', 'Game')
        assert len(calls) == 1 and 'GetSchemaForGame' in calls[0]
    asyncio.run(run())


class Client:
    configured = True
    def __init__(self):
        self.complete = False
        self.names = ['a', 'b']
        self.schema_calls = 0
        self.delay = 0
        self.active = self.peak = 0
    async def _get(self, path, **kw):
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(self.delay)
            if 'GetSchema' in path:
                self.schema_calls += 1
                return {'game': {'availableGameStats': {'achievements': [{'name': n} for n in self.names]}}}
            if 'GetRecently' in path:
                return {'response': {'total_count': 0}}
            return {'playerstats': {'success': True, 'achievements': [
                {'apiname': n, 'achieved': int(self.complete or n == 'a'), 'unlocktime': 100} for n in ['a', 'b']]}}
        finally:
            self.active -= 1


def make_monitor(tmp_path):
    items = [SimpleNamespace(steam_id='s', group_id=g, nickname=None, display_name='Player', avatar_url=None) for g in ['1', '2']]
    m = AchievementMonitor(SimpleNamespace(list_all=lambda: items), Client(), tmp_path / 'a.db')
    m.allowed = {'1', '2'}
    return m, items


@pytest.mark.parametrize('minutes', [0, 125])
def test_enrich_owned_playtime_uses_structured_filter(tmp_path, minutes):
    from unittest.mock import AsyncMock
    m, _ = make_monitor(tmp_path)
    m.client.game_name = AsyncMock(return_value='Game')
    m.client.game_cover = AsyncMock(return_value=None)
    m.client._get = AsyncMock(return_value={'response': {'games': [
        {'appid': 123, 'playtime_forever': minutes},
    ]}})
    result = asyncio.run(m.enrich('s', '123', {'title': 'Original'}))
    assert result['minutes'] == minutes
    m.client._get.assert_awaited_once()
    args = m.client._get.await_args
    assert args.args == ('IPlayerService/GetOwnedGames/v1/',)
    assert set(args.kwargs) == {'input_json'}
    assert json.loads(args.kwargs['input_json']) == {
        'steamid': 's', 'appids_filter': [123], 'include_played_free_games': True,
    }


@pytest.mark.parametrize('owned', [
    {'response': {}}, {'response': {'games': []}},
    {'response': {'games': [{'appid': 999, 'playtime_forever': 900}]}},
    TimeoutError('timeout'),
])
def test_enrich_uses_recent_lifetime_not_two_week_minutes(tmp_path, owned):
    from unittest.mock import AsyncMock
    m, _ = make_monitor(tmp_path)
    m.client.game_name = AsyncMock(return_value=None)
    m.client.game_cover = AsyncMock(return_value=None)
    m.client._get = AsyncMock(side_effect=[owned, {'response': {'games': [
        {'appid': 123, 'playtime_forever': 12345, 'playtime_2weeks': 12},
    ]}}])
    payload = {'title': 'Original'}
    result = asyncio.run(m.enrich('s', '123', payload))
    assert result['minutes'] == 12345 and 'minutes' not in payload
    args = m.client._get.await_args
    assert args.args == ('IPlayerService/GetRecentlyPlayedGames/v1/',)
    assert json.loads(args.kwargs['input_json']) == {'steamid': 's', 'count': 0}


@pytest.mark.parametrize('minutes', [None, -1, True, '123', 1.5])
def test_enrich_missing_or_invalid_minutes_remain_unknown(tmp_path, caplog, minutes):
    from unittest.mock import AsyncMock
    m, _ = make_monitor(tmp_path)
    m.client.game_name = AsyncMock(return_value=None)
    m.client.game_cover = AsyncMock(return_value=None)
    m.client._get = AsyncMock(return_value={'response': {'games': [
        {'appid': 123, 'playtime_forever': minutes, 'playtime_2weeks': 5},
    ]}})
    with caplog.at_level('INFO'):
        result = asyncio.run(m.enrich('s', '123', {'title': 'Original'}))
    assert 'minutes' not in result
    assert m.client._get.await_count == 2
    assert 'Achievement playtime missing' in caplog.text


def test_playtime_failure_logs_do_not_expose_secret_urls(tmp_path, caplog):
    from unittest.mock import AsyncMock
    m, _ = make_monitor(tmp_path)
    m.client.game_name = AsyncMock(return_value=None)
    m.client.game_cover = AsyncMock(return_value=None)
    m.client._get = AsyncMock(side_effect=RuntimeError('https://example.com/?key=SECRET'))
    result = asyncio.run(m.enrich('s', '123', {'title': 'Original'}))
    assert 'minutes' not in result
    assert 'RuntimeError' in caplog.text and 'SECRET' not in caplog.text


def test_schema_force_refresh_missing_data_no_false_complete(tmp_path):
    async def run():
        m, items = make_monitor(tmp_path)
        await m.check('s', 'a', 'Game')
        m.client.complete = True
        m.client.names.append('dlc')
        with pytest.raises(ValueError):
            await m.check('s', 'a', 'Game')
        assert not m.store.pending(time.time())
        m.client.names.remove('dlc')
        await m.check('s', 'a', 'Game')
        assert m.client.schema_calls == 3
        assert len(m.store.pending(time.time())) == 2
    asyncio.run(run())


def test_bounded_workers_timeout_cooldown_and_responsiveness(tmp_path):
    async def run():
        m, items = make_monitor(tmp_path)
        m.timeout = .03
        m.client.delay = 1
        m.store.target('s', 'a', 'Game', time.time())
        m.store.target('s', 'b', 'Game2', time.time())
        async def noop():
            pass
        m.deliver = noop
        ticks = []
        async def command():
            await asyncio.sleep(.01)
            ticks.append(True)
        await asyncio.gather(m.poll_once(), command())
        assert ticks and m.client.peak <= 2 and m.client.active == 0
        assert not m.store.due('check:s:a', time.time())
    asyncio.run(run())


def test_card_layout_and_missing_fields():
    from io import BytesIO
    from PIL import Image
    from qq_bot.steam_achievement_card import render_card
    for title in ['MONSTER HUNTER RISE', '长中文游戏名称' * 15, 'LONG TITLE ' * 25]:
        content = render_card('Player', title, 50, None, None, 1000)
        with Image.open(BytesIO(content)) as image:
            assert image.size == (1200, 800)


@pytest.mark.parametrize('failure', ['none', 'timeout', 'reject'])
def test_delivery_result_group_alias_and_retry(tmp_path, monkeypatch, failure):
    from qq_bot import steam_achievements as module
    from nonebot.adapters.onebot.v11 import ActionFailed
    async def run():
        m, items = make_monitor(tmp_path)
        items[0].nickname = '群一昵称'
        items[1].nickname = '群二昵称'
        m.store.observe('s', 'a', False, {}, ['1', '2'], 1)
        m.store.observe('s', 'a', True, {'title': 'Game'}, ['1', '2'], 2)
        sent = []
        class FakeBot:
            async def send_group_msg(self, **kw):
                sent.append(kw)
                if failure == 'timeout':
                    raise TimeoutError()
                if failure == 'reject':
                    raise ActionFailed(status='failed', retcode=100, wording='rejected', data=None)
        async def message(name, *args):
            return name
        async def enrich(sid, app, payload):
            return payload
        m.enrich = enrich
        monkeypatch.setattr(module, 'Bot', FakeBot)
        monkeypatch.setattr(module.nonebot, 'get_bots', lambda: {'bot': FakeBot()})
        monkeypatch.setattr(module, 'build_message', message)
        await m.deliver()
        assert {r['message'] for r in sent} == {'群一昵称', '群二昵称'}
        await m.deliver()
        assert len(sent) == 2
        with m.store.db() as db:
            states = {r[0] for r in db.execute('SELECT state FROM deliveries')}
        assert states == {{'none': 'sent', 'timeout': 'uncertain', 'reject': 'pending'}[failure]}
    asyncio.run(run())


def test_cancellation_releases_query_workers(tmp_path):
    async def run():
        m, _ = make_monitor(tmp_path)
        m.client.delay = 10
        task = asyncio.create_task(m.poll_once())
        for _ in range(100):
            if m.client.active:
                break
            await asyncio.sleep(.005)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert m.client.active == 0
    asyncio.run(run())

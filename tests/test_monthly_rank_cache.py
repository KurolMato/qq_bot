import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from nonebot.adapters.onebot.v11 import Message

from qq_bot import monthly_rank_cache as module
from qq_bot.game_time_tracker import LOCAL_TZ, GameTimeTracker


NOW = datetime(2026, 9, 18, 12, tzinfo=LOCAL_TZ)


def make_cache(tmp_path, monkeypatch, members=(1,)):
    source = SimpleNamespace(monthly_snapshot=Mock(return_value=SimpleNamespace(members=members)))
    render = AsyncMock(return_value=b'\x89PNG\r\nimage')
    monkeypatch.setattr(module, 'build_leaderboard_image', render)
    return module.MonthlyRankCache(source, tmp_path), source, render


def test_concurrent_requests_and_restart_reuse_daily_image(tmp_path, monkeypatch):
    cache, source, render = make_cache(tmp_path, monkeypatch)

    async def run():
        replies = await asyncio.gather(*(cache.message('100', at=NOW) for _ in range(5)))
        restarted = module.MonthlyRankCache(source, tmp_path)
        replies.append(await restarted.message('100', at=NOW + timedelta(hours=1)))
        assert all(reply == replies[0] for reply in replies)
        assert len(replies[0]) == 1 and replies[0][0].type == 'image'

    asyncio.run(run())
    source.monthly_snapshot.assert_called_once()
    render.assert_awaited_once()
    assert (tmp_path / '100/2026-09/main/2026-09-18.png').is_file()


def test_daily_rollover_is_at_four_am(tmp_path, monkeypatch):
    cache, source, _ = make_cache(tmp_path, monkeypatch)

    async def run():
        await cache.message('100', at=NOW)
        await cache.message('100', at=NOW.replace(day=19, hour=3, minute=59))
        assert source.monthly_snapshot.call_count == 1
        await cache.message('100', at=NOW.replace(day=19, hour=4))
        assert source.monthly_snapshot.call_count == 2

    asyncio.run(run())


def test_groups_boards_and_historical_year_are_isolated(tmp_path, monkeypatch):
    cache, source, _ = make_cache(tmp_path, monkeypatch)

    async def run():
        await cache.message('100', at=NOW)
        await cache.message('100', board='other', at=NOW)
        await cache.message('101', at=NOW)
        await cache.message('100', month=12, at=NOW)
        await cache.message('100', month=9, at=NOW)
        assert source.monthly_snapshot.call_count == 4

    asyncio.run(run())
    assert (tmp_path / '100/2025-12/main/2026-09-18.png').is_file()


def test_failed_refresh_keeps_previous_image(tmp_path, monkeypatch):
    cache, _, render = make_cache(tmp_path, monkeypatch)

    async def run():
        old = await cache.message('100', at=NOW)
        render.side_effect = RuntimeError('render unavailable')
        assert await cache.message('100', at=NOW + timedelta(days=1)) == old
        with pytest.raises(RuntimeError):
            await cache.message('101', at=NOW + timedelta(days=1))

    asyncio.run(run())
    assert not list(tmp_path.glob('**/2026-09-19.*'))


def test_empty_month_cached_until_next_day(tmp_path, monkeypatch):
    cache, source, render = make_cache(tmp_path, monkeypatch, members=())

    async def run():
        first = await cache.message('100', at=NOW)
        assert '还没有' in str(first)
        assert await cache.message('100', at=NOW) == first

    asyncio.run(run())
    source.monthly_snapshot.assert_called_once()
    render.assert_not_awaited()


def test_refresh_populates_both_boards_without_sending(tmp_path, monkeypatch):
    cache, _, _ = make_cache(tmp_path, monkeypatch)
    cache.message = AsyncMock()
    asyncio.run(cache.refresh(['100', '101']))
    assert cache.message.await_count == 4
    assert {call.kwargs['board'] for call in cache.message.await_args_list} == {'main', 'other'}


def test_rank_m_uses_cached_message(tmp_path, monkeypatch):
    import nonebot
    nonebot.init()
    from qq_bot import game_time_commands as commands
    cache = SimpleNamespace(message=AsyncMock(return_value=Message('cached')))
    monkeypatch.setattr(commands, 'monthly_cache', cache)
    monkeypatch.setattr(commands, 'config', SimpleNamespace(allowed_groups=set()))
    render = AsyncMock(side_effect=AssertionError('must not render in command'))
    monkeypatch.setattr(commands, 'build_leaderboard_message', render)
    bot = SimpleNamespace(send=AsyncMock())
    event = SimpleNamespace(group_id=100)
    asyncio.run(commands.handle_rank_command(bot, event, Message('o m 8')))
    cache.message.assert_awaited_once_with('100', month=8, board='other')
    bot.send.assert_awaited_once_with(event, Message('cached'))
    render.assert_not_awaited()


def test_group_discovery_uses_tracking_database(tmp_path):
    tracker = GameTimeTracker(tmp_path / 'tracking.db')
    assert tracker.group_ids() == []
    for group, account in [('100', 'a'), ('100', 'b'), ('101', 'a')]:
        for at in (NOW, NOW + timedelta(minutes=1)):
            tracker.observe(platform='steam', account_id=account, group_id=group,
                            display_name=account, avatar_url=None, game_key='game',
                            game_name='Game', game_image_url=None, observed_at=at)
    # Retained history must still be discoverable even after an account disappears.
    with tracker._connect() as db:
        db.execute("DELETE FROM game_time_accounts WHERE group_id='101'")
    assert set(tracker.group_ids()) == {'100', '101'}


def test_background_start_refreshes_allowed_groups_and_stops(monkeypatch):
    async def run():
        refreshed = asyncio.Event()
        refresh = AsyncMock(side_effect=lambda groups: refreshed.set())
        monkeypatch.setattr(module, 'monthly_cache', SimpleNamespace(refresh=refresh))
        monkeypatch.setattr(module.BotConfig, 'from_env', lambda: SimpleNamespace(allowed_groups={'100'}))
        await module.start_monthly_rank_cache()
        try:
            await asyncio.wait_for(refreshed.wait(), 1)
            refresh.assert_awaited_once_with({'100'})
        finally:
            await module.stop_monthly_rank_cache()
        assert module._task.done()

    asyncio.run(run())

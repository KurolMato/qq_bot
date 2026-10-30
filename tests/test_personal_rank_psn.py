import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from qq_bot import personal_rank_data
from qq_bot import personal_rank_psn as psn
from qq_bot.game_time_tracker import LOCAL_TZ, TrackedGame, month_start
from qq_bot.personal_rank_data import PersonalRankData
from qq_bot.ps_trophy_store import TrophyStore, read_platinum_summary

NOW = datetime(2026, 10, 4, 12, tzinfo=LOCAL_TZ)
END = datetime(2026, 10, 1, 3, 59, 59, 999999, tzinfo=LOCAL_TZ)


def seed(store, account, name, dates, *, observed=None):
    observed = observed or NOW.timestamp()
    store.state(account)
    store.begin(account, (0, 0, 0, len(dates)), observed)
    store.save_page(account, [dict(service='trophy2', title_id=name, platform='PS5', name=name,
        icon_url=None, counts=(0, 0, 0, len(dates)), updated=observed)], offset=1, finished=True)
    title = store.pending(account, 1)[0]
    store.save_trophies(title, [dict(id=str(i), kind='platinum', name=f'Platinum {i}', earned_at=date.timestamp())
                              for i, date in enumerate(dates)], observed)
    store.finish(account, observed, observed + 3600)


def profile(**kwargs):
    game = TrackedGame('ps', 'Game', 3600, None)
    base = PersonalRankData('Tester', None, 0, 0, 3600, (game,), (game,), None, None,
                            {('steam', 'Steam Game'): '全成就'}, END, True)
    return replace(base, **kwargs)


@pytest.fixture
def local_store(tmp_path, monkeypatch):
    path = tmp_path / 'ps-trophies.db'
    monkeypatch.setenv('PS_TROPHY_DB_PATH', str(path))
    monkeypatch.setattr(personal_rank_data, 'read_bound_account_ids', lambda *args: {'account'})
    return TrophyStore(path)


def test_card_reads_local_platinum_and_historical_cutoff_without_psn(local_store, monkeypatch):
    from qq_bot import ps5_registry
    constructor = Mock(side_effect=AssertionError('Card must not query PSN'))
    monkeypatch.setattr(ps5_registry, 'Ps5Client', constructor)
    seed(local_store, 'account', 'Game', [datetime(2026, 9, 12, tzinfo=timezone.utc),
                                       datetime(2026, 10, 2, tzinfo=timezone.utc)])
    result = asyncio.run(psn.enrich_personal_rank_psn(profile(), '100', tracker=object()))
    assert result.platinum_count == 1
    assert result.achievements == {('steam', 'Steam Game'): '全成就', ('ps', 'Game'): '白金'}
    constructor.assert_not_called()


def test_later_platinum_does_not_give_historical_badge(local_store):
    seed(local_store, 'account', 'Game', [datetime(2026, 10, 2, tzinfo=timezone.utc)])
    result = asyncio.run(psn.enrich_personal_rank_psn(profile(), '100', tracker=object()))
    assert result.platinum_count == 0
    assert ('ps', 'Game') not in result.achievements


def test_old_platinum_counts_zero_this_month_but_keeps_badge(local_store):
    seed(local_store, 'account', 'Game', [datetime(2026, 8, 12, tzinfo=timezone.utc)])
    result = asyncio.run(psn.enrich_personal_rank_psn(profile(), '100', tracker=object()))
    assert result.platinum_count == 0
    assert result.achievements[('ps', 'Game')] == '白金'


def test_missing_or_incomplete_baseline_stays_unknown_without_creating_db(local_store):
    result = asyncio.run(psn.enrich_personal_rank_psn(profile(), '100', tracker=object()))
    assert result.platinum_count is None
    assert not local_store.path.exists()
    local_store.state('account')
    result = asyncio.run(psn.enrich_personal_rank_psn(profile(), '100', tracker=object()))
    assert result.platinum_count is None


def test_multiple_accounts_require_complete_baselines_and_merge_dates(local_store, monkeypatch):
    monkeypatch.setattr(personal_rank_data, 'read_bound_account_ids', lambda *args: {'a', 'b'})
    seed(local_store, 'a', 'Game', [datetime(2026, 9, 12, tzinfo=timezone.utc)])
    result = asyncio.run(psn.enrich_personal_rank_psn(profile(), '100', tracker=object()))
    assert result.platinum_count is None
    seed(local_store, 'b', 'Other', [datetime(2026, 9, 15, tzinfo=timezone.utc)])
    result = asyncio.run(psn.enrich_personal_rank_psn(profile(), '100', tracker=object()))
    assert result.platinum_count == 2


def test_inflight_new_unlocks_are_hidden_until_complete_snapshot(local_store):
    earlier = NOW.timestamp() - 100
    seed(local_store, 'account', 'Game', [datetime(2026, 9, 12, tzinfo=timezone.utc)], observed=earlier)
    local_store.begin('account', (0, 0, 0, 2), NOW.timestamp())
    local_store.save_page('account', [dict(service='trophy2', title_id='Game', platform='PS5', name='Game',
        icon_url=None, counts=(0, 0, 0, 2), updated=NOW.timestamp())], offset=1, finished=True)
    title = local_store.pending('account', 1)[0]
    local_store.save_trophies(title, [dict(id='1', kind='platinum', name='New',
        earned_at=datetime(2026, 9, 20, tzinfo=timezone.utc).timestamp())], NOW.timestamp())
    assert read_platinum_summary(local_store.path, ('account',), month_start(END), END)[0] == 1
    local_store.finish('account', NOW.timestamp() + 1, NOW.timestamp() + 3600)
    assert read_platinum_summary(local_store.path, ('account',), month_start(END), END)[0] == 2


@pytest.mark.parametrize('has_ps', [False, True])
def test_unlinked_account_is_zero_only_when_no_ps_games(local_store, monkeypatch, has_ps):
    monkeypatch.setattr(personal_rank_data, 'read_bound_account_ids', lambda *args: set())
    data = profile() if has_ps else profile(games=(), recent_games=())
    result = asyncio.run(psn.enrich_personal_rank_psn(data, '100', tracker=object()))
    assert result.platinum_count == (None if has_ps else 0)


def test_0400_month_boundary_and_actual_unlock_time(local_store):
    seed(local_store, 'account', 'Game', [END, END + timedelta(microseconds=1)])
    assert read_platinum_summary(local_store.path, ('account',), month_start(END), END)[0] == 1
    oct_start = END + timedelta(microseconds=1)
    assert read_platinum_summary(local_store.path, ('account',), oct_start, NOW)[0] == 1

import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace as NS

import pytest
from psnawp_api.models.trophies import PlatformType

from qq_bot import ps_trophies as ps
from qq_bot.game_time_tracker import LOCAL_TZ, month_start
from qq_bot.ps_trophy_store import KINDS, TrophyStore, read_platinum_summary

NOW = datetime(2026, 10, 4, 12, tzinfo=LOCAL_TZ)
UNLOCK = datetime(2026, 9, 30, 23, tzinfo=LOCAL_TZ)


def trophies(*kinds):
    return [NS(trophy_id=index, trophy_type=NS(value=kind), earned=True,
               trophy_name=f'Trophy {index}', earned_date_time=UNLOCK)
            for index, kind in enumerate(kinds)]


def total(values):
    return NS(**{kind: sum(t.earned and t.trophy_type.value == kind for t in values) for kind in KINDS})


class User:
    def __init__(self, games):
        self.games = games
        self.summary_calls, self.pages, self.details = 0, [], []
        self.error = None
        self.summary_override = None
        self.hidden = False

    def trophy_summary(self):
        self.summary_calls += 1
        return NS(earned_trophies=self.summary_override or total([t for game in self.games.values() for t in game]))

    def trophy_titles(self, *, limit, offset, page_size):
        self.pages.append(offset)
        values = [NS(np_service_name='trophy2', np_communication_id=key, title_name=key,
                     title_icon_url=None, title_platform={PlatformType.PS5}, hidden_flag=self.hidden,
                     earned_trophies=total(game), last_updated_datetime=UNLOCK)
                  for key, game in self.games.items()]
        return iter(values[offset:limit])

    def trophies(self, title_id, platform, **kwargs):
        self.details.append(title_id)
        assert platform is PlatformType.PS5
        assert kwargs['trophy_group_id'] == 'all'
        assert kwargs['include_progress'] is True
        if self.error:
            raise self.error
        return iter(self.games[title_id])


class Client:
    configured = True

    def __init__(self, users):
        self.users = users

    def _get_api(self):
        return NS(user=lambda account_id: self.users[account_id])

    async def _call(self, operation, callback):
        return callback()


@pytest.fixture
def clock(monkeypatch):
    current = [NOW.timestamp()]
    monkeypatch.setattr(ps.time, 'time', lambda: current[0])
    return current


def monitor(tmp_path, user, accounts=('a',)):
    source = NS(list_all=lambda: [NS(account_id=account, group_id='100') for account in accounts])
    worker = ps.TrophyMonitor(source, Client({'a': user}), tmp_path / 'ps.db')
    worker.allowed = set()
    return worker


async def complete(worker, account='a'):
    for _ in range(20):
        await worker.step(account)
        state = worker.store.state(account)
        if state['counts'] is not None and state['stage'] == 'idle' and not state['last_error']:
            return state
    raise AssertionError(worker.store.state(account))


def test_initial_sync_saves_all_kinds_unlock_dates_and_survives_restart(tmp_path, clock):
    user = User({'Game': trophies('bronze', 'silver', 'gold', 'platinum')})
    worker = monitor(tmp_path, user)
    state = asyncio.run(complete(worker))
    assert json.loads(state['counts']) == [1, 1, 1, 1]
    with worker.store.db() as db:
        rows = db.execute('SELECT kind,earned_at FROM trophies').fetchall()
    assert {row['kind'] for row in rows} == set(KINDS)
    assert all(row['earned_at'] == UNLOCK.timestamp() for row in rows)
    end = datetime(2026, 10, 1, 3, 59, 59, 999999, tzinfo=LOCAL_TZ)
    assert read_platinum_summary(worker.store.path, ('a',), month_start(end), end) == (1, frozenset({'game'}))
    restarted = monitor(tmp_path, user)
    before = user.summary_calls
    asyncio.run(restarted.step('a'))
    assert user.summary_calls == before
    assert restarted.store.state('a')['due'] == datetime(2026, 10, 4, 16, 10, tzinfo=LOCAL_TZ).timestamp()


def test_daily_check_skips_details_when_counts_unchanged(tmp_path, clock):
    user = User({'Game': trophies('platinum')})
    worker = monitor(tmp_path, user)
    state = asyncio.run(complete(worker))
    pages, details = list(user.pages), list(user.details)
    clock[0] = state['due']
    asyncio.run(worker.step('a'))
    assert user.pages == pages and user.details == details
    assert worker.store.state('a')['due'] == datetime(2026, 10, 5, 4, 10, tzinfo=LOCAL_TZ).timestamp()


def test_incremental_sync_fetches_only_changed_game_without_duplicate_unlocks(tmp_path, clock):
    user = User({'Old': trophies('platinum'), 'New': trophies('bronze')})
    worker = monitor(tmp_path, user)
    state = asyncio.run(complete(worker))
    user.details.clear()
    user.games['New'] = trophies('bronze', 'silver', 'platinum')
    clock[0] = state['due']
    state = asyncio.run(complete(worker))
    assert user.details == ['New']
    assert json.loads(state['counts']) == [1, 1, 0, 2]
    with worker.store.db() as db:
        assert db.execute('SELECT COUNT(*) FROM trophies').fetchone()[0] == 4


def test_initial_listing_and_detail_batches_resume_after_restart(tmp_path, clock, monkeypatch):
    monkeypatch.setattr(ps, 'PAGE_SIZE', 2)
    monkeypatch.setattr(ps, 'DETAIL_BATCH_SIZE', 1)
    user = User({f'Game{i}': trophies('platinum') for i in range(5)})
    worker = monitor(tmp_path, user)
    async def partial():
        await worker.step('a')
        await worker.step('a')
    asyncio.run(partial())
    assert worker.store.state('a')['offset'] == 2
    assert worker.store.state('a')['counts'] is None
    worker = monitor(tmp_path, user)
    state = asyncio.run(complete(worker))
    assert user.pages == [0, 2, 4]
    assert len(user.details) == 5
    assert json.loads(state['counts'])[-1] == 5


@pytest.mark.parametrize('title_count', [101, 200, 205])
def test_sdk_pagination_keeps_positive_http_limit_after_restart(tmp_path, clock, monkeypatch, title_count):
    from psnawp_api.core import PSNAWPBadRequestError
    from psnawp_api.models.user import User as PSNUser

    monkeypatch.setattr(ps, 'DETAIL_BATCH_SIZE', title_count)
    user = User({f'Game{i}': trophies('platinum') for i in range(title_count)})
    requests = []

    def get(*, url, params):
        assert url.endswith('/users/a/trophyTitles')
        requests.append(dict(params))
        if not 1 <= params['limit'] <= 800:
            raise PSNAWPBadRequestError('Bad Request (query: limit)')
        start, end = params['offset'], params['offset'] + params['limit']
        payload = dict(totalItemCount=title_count, nextOffset=end if end < title_count else None,
            trophyTitles=[dict(npServiceName='trophy2', npCommunicationId=name,
                trophyTitleName=name, trophyTitlePlatform='PS5', hiddenFlag=False,
                earnedTrophies=vars(total(game)), lastUpdatedDateTime=UNLOCK.isoformat())
                for name, game in list(user.games.items())[start:end]])
        return NS(json=lambda: payload)

    # Exercise the installed SDK's limit/offset conversion and iterator, while
    # keeping every HTTP request and trophy detail local to the test.
    user.trophy_titles = PSNUser(NS(get=get), 'TestUser', 'a').trophy_titles
    worker = monitor(tmp_path, user)

    async def first_page():
        await worker.step('a')
        await worker.step('a')

    asyncio.run(first_page())
    assert worker.store.state('a')['offset'] == 100
    worker = monitor(tmp_path, user)
    state = asyncio.run(complete(worker))
    assert requests == [dict(limit=100, offset=offset) for offset in range(0, title_count + 1, 100)]
    assert json.loads(state['counts']) == [0, 0, 0, title_count]
    assert set(user.details) == set(user.games)
    with worker.store.db() as db:
        assert db.execute('SELECT COUNT(*) FROM trophies').fetchone()[0] == title_count


def test_failed_details_keep_baseline_and_recover_without_losing_changes(tmp_path, clock):
    user = User({'Game': trophies('platinum')})
    worker = monitor(tmp_path, user)
    state = asyncio.run(complete(worker))
    clock[0] = state['due']
    user.games['Game'] = trophies('platinum', 'bronze')
    user.error = RuntimeError('temporary network outage')
    async def fail():
        for _ in range(3):
            await worker.step('a')
    asyncio.run(fail())
    state = worker.store.state('a')
    assert json.loads(state['counts']) == [0, 0, 0, 1]
    assert state['stage'] == 'details' and state['last_error'] == 'RuntimeError'
    assert read_platinum_summary(worker.store.path, ('a',), month_start(UNLOCK), NOW)[0] == 1
    clock[0] = state['retry_at']
    user.error = None
    state = asyncio.run(complete(worker))
    assert json.loads(state['counts']) == [1, 0, 0, 1]


def test_counts_decreasing_do_not_overwrite_saved_trophies(tmp_path, clock):
    user = User({'Game': trophies('bronze', 'platinum')})
    worker = monitor(tmp_path, user)
    state = asyncio.run(complete(worker))
    clock[0] = state['due']
    user.summary_override = NS(bronze=0, silver=0, gold=0, platinum=0)
    asyncio.run(worker.step('a'))
    assert json.loads(worker.store.state('a')['counts']) == [1, 0, 0, 1]
    assert worker.store.state('a')['last_error'] == 'ValueError'


def test_hidden_title_and_incomplete_detail_never_become_complete_zero(tmp_path, clock):
    user = User({'Game': trophies('platinum')})
    user.hidden = True
    worker = monitor(tmp_path, user)
    async def fail():
        await worker.step('a')
        await worker.step('a')
    asyncio.run(fail())
    assert worker.store.state('a')['counts'] is None
    assert read_platinum_summary(worker.store.path, ('a',), month_start(UNLOCK), NOW) is None
    user.hidden = False
    user.games['Game'][0].earned_date_time = None
    clock[0] = worker.store.state('a')['retry_at']
    asyncio.run(fail())
    assert worker.store.state('a')['counts'] is None


def test_changed_during_scan_restarts_discovery_and_recovers(tmp_path, clock):
    user = User({'Game': trophies('bronze')})
    worker = monitor(tmp_path, user)
    async def discover():
        await worker.step('a')
        await worker.step('a')
    asyncio.run(discover())
    user.games['Game'] = trophies('bronze', 'platinum')
    asyncio.run(worker.step('a'))
    state = worker.store.state('a')
    assert state['stage'] == 'idle' and state['counts'] is None
    assert state['last_error'] == 'TrophyChanged'
    clock[0] = state['retry_at']
    state = asyncio.run(complete(worker))
    assert json.loads(state['counts']) == [1, 0, 0, 1]


def test_duplicate_accounts_and_group_filter_do_not_duplicate_requests(tmp_path, clock):
    user = User({})
    worker = monitor(tmp_path, user)
    worker.registry.list_all = lambda: [NS(account_id='a', group_id='100'),
        NS(account_id='a', group_id='200'), NS(account_id='excluded', group_id='300')]
    worker.allowed = {'100', '200'}
    asyncio.run(worker.poll_once())
    assert user.summary_calls == 1
    worker.client.configured = False
    asyncio.run(worker.poll_once())
    assert user.summary_calls == 1


def test_zero_trophies_are_valid_after_complete_initial_sync(tmp_path, clock):
    worker = monitor(tmp_path, User({}))
    asyncio.run(complete(worker))
    assert read_platinum_summary(worker.store.path, ('a',), month_start(UNLOCK), NOW) == (0, frozenset())


def test_one_unreadable_account_does_not_stop_other_accounts(tmp_path, clock):
    worker = monitor(tmp_path, User({}), accounts=('a', 'b'))
    bad = User({})
    bad.summary_override = NS(bronze=None, silver=0, gold=0, platinum=0)
    worker.client.users['a'] = bad
    good = User({})
    worker.client.users['b'] = good
    asyncio.run(worker.poll_once())
    assert worker.store.state('a')['last_error'] == 'ValueError'
    assert worker.store.state('b')['stage'] == 'listing'
    assert good.summary_calls == 1


def test_partial_game_detail_success_is_retained_when_next_game_fails(tmp_path, clock, monkeypatch):
    monkeypatch.setattr(ps, 'DETAIL_BATCH_SIZE', 1)
    user = User({'First': trophies('platinum'), 'Second': trophies('platinum')})
    worker = monitor(tmp_path, user)
    async def first_batch():
        for _ in range(3):
            await worker.step('a')
    asyncio.run(first_batch())
    assert worker.store.state('a')['counts'] is None
    assert user.details == ['First']
    user.error = RuntimeError('temporary outage')
    asyncio.run(worker.step('a'))
    assert user.details == ['First', 'Second']
    worker = monitor(tmp_path, user)
    clock[0] = worker.store.state('a')['retry_at']
    user.error = None
    asyncio.run(complete(worker))
    assert user.details == ['First', 'Second', 'Second']


def test_visible_title_totals_mismatch_retries_with_backoff_not_false_zero(tmp_path, clock):
    user = User({})
    user.summary_override = NS(bronze=0, silver=0, gold=0, platinum=1)
    worker = monitor(tmp_path, user)
    async def scan():
        for _ in range(3):
            await worker.step('a')
    asyncio.run(scan())
    first = worker.store.state('a')
    assert first['stage'] == 'idle' and first['counts'] is None
    clock[0] = first['retry_at']
    asyncio.run(scan())
    second = worker.store.state('a')
    assert second['failures'] == 2
    assert second['retry_at'] - clock[0] == 120


def test_earned_trophy_validation_rejects_missing_dates_and_incomplete_counts():
    values = trophies('platinum')
    values[0].earned_date_time = None
    with pytest.raises(ValueError, match='unlock time'):
        ps.parse_trophies(values, (0, 0, 0, 1))
    with pytest.raises(ps.TrophyChanged):
        ps.parse_trophies([], (0, 0, 0, 1))


def test_persistent_validation_mismatch_backs_off_and_logs_reason(tmp_path, clock, caplog):
    user = User({})
    user.summary_override = NS(bronze=0, silver=0, gold=0, platinum=1)
    worker = monitor(tmp_path, user)
    for delay in (60, 120, 240, 43200, 43200):
        async def scan():
            for _ in range(3):
                await worker.step('a')
        asyncio.run(scan())
        state = worker.store.state('a')
        assert state['retry_at'] - clock[0] == delay
        assert state['counts'] is None
        assert state['stage'] == 'idle'
        clock[0] = state['retry_at']
        worker = monitor(tmp_path, user)
    assert 'Visible trophy titles do not match account totals' in caplog.text
    assert 'visible=(0, 0, 0, 0) account=(0, 0, 0, 1)' in caplog.text
    user.summary_override = None
    state = asyncio.run(complete(worker))
    assert state['failures'] == 0 and json.loads(state['counts']) == [0, 0, 0, 0]


def test_network_trophy_failure_keeps_shorter_capped_backoff(tmp_path):
    store = TrophyStore(tmp_path / 'network.db')
    store.state('a')
    delays = [store.failed('a', 1000, 'Ps5Error') for _ in range(10)]
    assert delays[:3] == [60, 120, 240]
    assert delays[-1] == 3600


def test_ps4_title_retains_service_and_platform():
    user = User({'Game': trophies('platinum')})
    title = next(user.trophy_titles(limit=1, offset=0, page_size=1))
    title.np_service_name = 'trophy'
    title.title_platform = {PlatformType.PS4, PlatformType.PS3}
    result = ps.parse_title(title)
    assert result['service'] == 'trophy'
    assert result['platform'] in {'PS3', 'PS4'}


def test_conflicting_saved_trophy_identity_rolls_back_batch(tmp_path, clock):
    worker = monitor(tmp_path, User({'Game': trophies('platinum')}))
    asyncio.run(complete(worker))
    worker.store.begin('a', (1, 0, 0, 1), clock[0])
    worker.store.save_page('a', [dict(service='trophy2', title_id='Game', platform='PS5', name='Game',
        icon_url=None, counts=(1, 0, 0, 1), updated=clock[0])], offset=1, finished=True)
    title = worker.store.pending('a', 1)[0]
    with pytest.raises(ValueError, match='identities disagree'):
        worker.store.save_trophies(title, [dict(id='0', kind='bronze', name='Conflict', earned_at=UNLOCK.timestamp())], clock[0])
    with worker.store.db() as db:
        assert db.execute('SELECT COUNT(*) FROM trophies').fetchone()[0] == 1
    assert worker.store.pending('a', 1)


def test_monitor_start_and_stop_uses_separate_client_and_closes_worker(monkeypatch):
    from unittest.mock import Mock
    from qq_bot import ps5_registry
    client = Client({})
    client.configured = False
    client._executor = NS(shutdown=Mock())
    constructor = Mock(return_value=client)
    monkeypatch.setattr(ps5_registry, 'Ps5Client', constructor)
    monkeypatch.setattr(ps5_registry, 'registry', NS(list_all=lambda: []))
    async def lifecycle():
        await ps.start_trophy_monitor()
        assert ps._monitor.client is client
        await asyncio.sleep(0)
        await ps.stop_trophy_monitor()
        assert ps._task is None and ps._monitor is None
    asyncio.run(lifecycle())
    constructor.assert_called_once_with()
    client._executor.shutdown.assert_called_once_with(wait=False, cancel_futures=True)


def test_trophy_store_relative_path_is_project_based(monkeypatch):
    from qq_bot.ps_trophy_store import PROJECT_ROOT, trophy_db_path
    monkeypatch.setenv('PS_TROPHY_DB_PATH', 'data/custom-trophies.db')
    assert trophy_db_path() == PROJECT_ROOT / 'data' / 'custom-trophies.db'


def test_bot_lifecycle_registers_both_trophy_hooks():
    import ast
    from pathlib import Path
    source = Path(__file__).resolve().parents[1] / 'qq_bot' / 'run.py'
    tree = ast.parse(source.read_text(encoding='utf-8-sig'))
    hooks = {(node.func.attr, node.args[0].id) for node in ast.walk(tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
             and node.func.attr in {'on_startup', 'on_shutdown'} and node.args
             and isinstance(node.args[0], ast.Name)}
    assert ('on_startup', 'start_trophy_monitor') in hooks
    assert ('on_shutdown', 'stop_trophy_monitor') in hooks


@pytest.mark.parametrize('hour,minute,next_day,next_hour,next_minute', [
    (4, 9, 4, 4, 10), (4, 10, 4, 16, 10), (16, 9, 4, 16, 10), (16, 10, 5, 4, 10),
])
def test_schedule_uses_fixed_utc8_slots(hour, minute, next_day, next_hour, next_minute):
    at = NOW.replace(hour=hour, minute=minute)
    assert datetime.fromtimestamp(ps.next_check(at.timestamp()), LOCAL_TZ) == NOW.replace(
        day=next_day, hour=next_hour, minute=next_minute)


@pytest.mark.parametrize('invalid', [-1, None, True, 1.5])
def test_invalid_count_is_rejected(invalid):
    with pytest.raises(ValueError):
        ps.counts(NS(bronze=invalid, silver=0, gold=0, platinum=0))


def test_read_does_not_create_database_or_expose_partial_snapshot(tmp_path):
    path = tmp_path / 'not-created.db'
    assert read_platinum_summary(path, ('a',), month_start(UNLOCK), NOW) is None
    assert not path.exists()
    store = TrophyStore(path)
    store.state('a')
    store.begin('a', (0, 0, 0, 1), NOW.timestamp())
    with pytest.raises(ValueError):
        store.finish('a', NOW.timestamp(), NOW.timestamp() + 3600)
    assert store.state('a')['counts'] is None

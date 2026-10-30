import asyncio
from datetime import datetime, timezone
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image, ImageDraw

from qq_bot.steam_achievement_store import AchievementStore
from qq_bot.steam_achievements import AchievementMonitor
from qq_bot.steam_achievement_ui import render_card


def monitor(tmp_path):
    m = AchievementMonitor(SimpleNamespace(list_all=lambda: [SimpleNamespace(steam_id='1', group_id='2')]),
                           SimpleNamespace(), tmp_path / 'achievements.db')
    m.allowed = {'2'}
    return m


@pytest.mark.parametrize('times,expected', [([100,200], 'b'), ([200,200], 'a'), ([0,200], None),
                                          ([True,200], None), ([float('nan'),200], None),
                                          ([100,999999999999], None)])
def test_completion_payload_preserves_last_details_and_valid_timeline(tmp_path, times, expected):
    m = monitor(tmp_path)
    m.store.observe('1','123',False,{},['2'],1)
    calls = []

    async def api(path, **kwargs):
        calls.append((path,kwargs))
        if 'GetSchema' in path:
            return {'game': {'availableGameStats': {'achievements': [
                {'name': k, 'displayName': f'成就{k}', 'description': f'描述{k}', 'icon': f'https://example.com/{k}.png'}
                for k in ('a','b')
            ]}}}
        return {'playerstats': {'success': True, 'achievements': [
            {'apiname': k, 'achieved': 1, 'unlocktime': stamp} for k,stamp in zip(('a','b'),times)
        ]}}

    m.client._get = api
    asyncio.run(m.check('1','123','Game'))
    payload = m.store.pending(9999999999)[0]['payload']
    assert not any('GetGlobal' in path for path,_ in calls)
    assert all(kw['l'] == 'schinese' for _,kw in calls)
    if expected:
        assert payload['last_achievement_id'] == expected
        assert payload['achievement_name'] == f'成就{expected}'
        assert payload['achievement_description'] == f'描述{expected}'
        assert payload['achievement_icon'].endswith(f'{expected}.png')
        assert payload['first_achieved_at'] == min(times)
        assert payload['achieved_at'] == max(times)
        assert payload['last_achievement_count'] == (2 if times[0] == times[1] else 1)
    else:
        assert 'last_achievement_id' not in payload
        assert 'first_achieved_at' not in payload
        assert payload['achieved_at'] is None


def test_percentage_cache_survives_restart_and_expires(tmp_path, monkeypatch):
    from qq_bot import steam_achievements as module
    now = [1000]
    monkeypatch.setattr(module.time, 'time', lambda: now[0])
    m = monitor(tmp_path)
    m.client._get = AsyncMock(return_value={'achievementpercentages': {'achievements': [
        {'name':'a','percent':'3.8'}, {'name':'b','percent':0},
        {'name':'bad','percent':True}, {'name':'nan','percent':'NaN'}, {'name':'high','percent':101},
    ]}})
    assert asyncio.run(m.global_percentage('123','a')) == 3.8
    restarted = monitor(tmp_path)
    restarted.client = m.client
    assert asyncio.run(restarted.global_percentage('123','b')) == 0
    assert asyncio.run(restarted.global_percentage('123','bad')) is None
    assert asyncio.run(restarted.global_percentage('123','nan')) is None
    assert asyncio.run(restarted.global_percentage('123','high')) is None
    m.client._get.assert_awaited_once_with('ISteamUserStats/GetGlobalAchievementPercentagesForApp/v2/',gameid='123')
    now[0] += 86401
    assert asyncio.run(restarted.global_percentage('123','a')) == 3.8
    assert m.client._get.await_count == 2


def test_percentage_failure_does_not_block_enrich_and_is_backed_off(tmp_path, caplog):
    m = monitor(tmp_path)
    m.client.game_name = AsyncMock(return_value='Game')
    m.client.game_cover = AsyncMock(return_value=None)
    global_calls = []

    async def api(path, **kwargs):
        if 'GetGlobal' in path:
            global_calls.append(path)
            raise TimeoutError('https://example.com/?key=SECRET')
        return {'response': {'games': [{'appid':123,'playtime_forever':99}]}}

    m.client._get = api
    payload = {'title':'Game', 'last_achievement_id':'a'}
    assert asyncio.run(m.enrich('1','123',payload))['minutes'] == 99
    assert asyncio.run(m.enrich('1','123',payload))['achievement_percent'] is None
    assert len(global_calls) == 1 and 'SECRET' not in caplog.text


def test_metadata_table_upgrade_keeps_existing_outbox(tmp_path):
    import sqlite3
    path = tmp_path / 'old.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE events (sid TEXT, app TEXT, payload TEXT, PRIMARY KEY(sid,app))')
        db.execute('INSERT INTO events VALUES (?,?,?)',('1','123','{"title":"Old"}'))
    store = AchievementStore(path)
    store.save_card_metadata('123','percentages',{'a':5},1000)
    assert store.card_metadata('123','percentages',999) == {'a':5}
    assert store.card_metadata('123','percentages',1000) is None
    with store.db() as db:
        assert db.execute('SELECT payload FROM events').fetchone()[0] == '{"title":"Old"}'


@pytest.mark.parametrize('percent', [0, 3.8, None, float('nan'), 0.01])
def test_renderer_fields_no_rare_block_and_alignment(monkeypatch, percent):
    texts = []
    original = ImageDraw.ImageDraw.text

    def capture(self, xy, text, *args, **kwargs):
        texts.append((xy,text,kwargs))
        return original(self,xy,text,*args,**kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw,'text',capture)
    stamp = datetime(2026,10,4,tzinfo=timezone.utc).timestamp()
    content = render_card('昵称很长'*20,'长游戏名称'*20,12345,12345678,stamp,stamp,
                          first_achieved_at=stamp-86400,achievement_name='很长的成就名称'*20,
                          achievement_description='很长的成就描述'*50,achievement_percent=percent)
    with Image.open(BytesIO(content)) as image:
        assert image.size == (1400,910) and image.format == 'JPEG'
    assert len(content) < 2_000_000
    assert not any('最稀有' in value or '历时' in value for _,value,_ in texts)
    assert any(value == '最后解锁' for _,value,_ in texts)
    # The two captions and their values share a left edge, within glyph bearing.
    for caption, value in [('成就解锁','12345'), ('累计游玩','205761')]:
        label = next(xy for xy,t,_ in texts if t == caption)
        number = next(xy for xy,t,_ in texts if t.startswith(value))
        assert abs(label[0]-number[0]) <= 2
    expected = '—' if percent is None or percent != percent else '<0.1%' if 0 < percent < .1 else f'{percent:.1f}%'
    assert any(t == expected for _,t,_ in texts)


def test_old_payload_missing_dates_uses_detection_label(monkeypatch):
    texts = []
    original = ImageDraw.ImageDraw.text
    def capture(self, xy, text, *args, **kwargs):
        texts.append(text)
        return original(self,xy,text,*args,**kwargs)
    monkeypatch.setattr(ImageDraw.ImageDraw,'text',capture)
    render_card('Name','Game',50,None,None,1000)
    assert '检测到全成就' in texts and '未记录' in texts
    assert '成就详情暂不可用' in texts


def test_message_uses_cached_assets_and_one_image(monkeypatch):
    from qq_bot import steam_achievement_card as card
    load = AsyncMock(return_value=None)
    monkeypatch.setattr(card,'_load_image',load)
    payload = dict(title='Game',total=2,detected_at=1000,achievement_icon='https://example.com/icon',
                   achievement_name='Last',achievement_percent=3.8)
    message = asyncio.run(card.build_message('Name',payload,'https://example.com/avatar'))
    assert sum(segment.type == 'image' for segment in message) == 1
    assert sum(segment.type == 'text' for segment in message) == 1
    assert load.await_count == 3
    assert {call.args[0] for call in load.await_args_list} == {None,'https://example.com/icon','https://example.com/avatar'}

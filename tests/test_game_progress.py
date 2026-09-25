import asyncio
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import nonebot
import pytest
from PIL import Image

from qq_bot.game_progress import ProgressService, GameProgress, PlayerProgress, render_progress, build_progress_messages
from qq_bot.game_calendar import GameLookupError


def member(sid):
    return SimpleNamespace(steam_id=sid, nickname='群友'+sid, display_name='Steam'+sid, avatar_url=None)


class Client:
    configured = True

    async def game_details(self, app):
        return {'type': 'game', 'name': '示例游戏'}

    async def _public_get(self, *a, **kw):
        return {'items': [{'type': 'app', 'id': 10, 'name': 'Example One'},
                          {'type': 'app', 'id': 20, 'name': 'Example Two'}]}

    async def _get(self, path, **kw):
        if 'Schema' in path:
            return {'game': {'availableGameStats': {'achievements': [{'name': 'a'}, {'name': 'b'}]}}}
        sid = kw['steamid']
        if 'Owned' in path:
            return {'response': {'games': [{'appid': 10, 'playtime_forever': 123}]}}
        if sid == 'private':
            raise RuntimeError('private')
        values = [{'apiname': 'a', 'achieved': 1, 'unlocktime': 100},
                  {'apiname': 'b', 'achieved': int(sid != 'partial'), 'unlocktime': 200 if sid != 'unknown' else 0}]
        if sid == 'mismatch':
            values.pop()
        return {'playerstats': {'success': True, 'achievements': values}}


def test_group_scope_sort_private_and_incomplete_schema():
    def list_group(gid):
        assert gid == 'group-1'
        return [member(v) for v in ['partial', 'private', 'unknown', 'full', 'mismatch']]
    result = asyncio.run(ProgressService(Client(), SimpleNamespace(list_group=list_group)).query('group-1', '10'))
    assert [v.name for v in result.players] == ['群友full', '群友unknown', '群友partial']
    assert result.unavailable == 2
    assert result.players[0].completed_at == 200
    assert result.players[1].completed_at is None
    assert result.players[-1].unlocked == 1
    assert result.players[0].minutes == 123


def test_ambiguous_lookup_and_exact_name():
    service = ProgressService(Client(), None)
    with pytest.raises(GameLookupError, match='AppID'):
        asyncio.run(service.resolve('Example'))
    assert asyncio.run(service.resolve('Example Two'))[0] == '20'


def test_no_members_and_no_schema():
    service = ProgressService(Client(), SimpleNamespace(list_group=lambda _: []))
    with pytest.raises(GameLookupError, match='登记'):
        asyncio.run(service.query('g', '10'))
    service.registry.list_group = lambda _: [member('full')]
    service.client._get = AsyncMock(return_value={})
    with pytest.raises(GameLookupError, match='没有可查询'):
        asyncio.run(service.query('g', '10'))


def test_missing_playtime_does_not_hide_completion():
    client = Client()
    original = client._get
    async def get(path, **kw):
        if 'Owned' in path:
            raise RuntimeError('private library')
        return await original(path, **kw)
    client._get = get
    result = asyncio.run(ProgressService(client, None).player(member('full'), '10', {'a', 'b'}))
    assert result.complete and result.minutes is None


def test_render_and_pagination(monkeypatch):
    import qq_bot.game_progress as module
    players = [PlayerProgress('超长中文昵称' * 10, 'account' * 30, None, 2, 2, 100, 123)] * 13
    result = GameProgress('10', '超长游戏标题' * 20, None, players, 2)
    with Image.open(BytesIO(render_progress(result, players[:12], {}))) as image:
        assert image.width == 1200 and image.height < 2500
    monkeypatch.setattr(module, '_load_image', AsyncMock(return_value=None))
    async def collect():
        return [v async for v in build_progress_messages(result)]
    assert len(asyncio.run(collect())) == 2


def test_mention_routing_and_allowed_group(monkeypatch):
    nonebot.init()
    from qq_bot import game_commands as commands
    result = GameProgress('10', 'Example', None, [])
    query = AsyncMock(return_value=result)
    monkeypatch.setattr(commands, 'progress_service', SimpleNamespace(query=query))
    monkeypatch.setattr(commands, 'config', SimpleNamespace(allowed_groups={'1'}))
    async def messages(result):
        yield 'image'
    monkeypatch.setattr(commands, 'build_progress_messages', messages)
    bot = SimpleNamespace(send=AsyncMock())
    event = SimpleNamespace(group_id=1, get_plaintext=lambda: 'game Example Two')
    asyncio.run(commands.handle_mentioned_game(bot, event))
    query.assert_awaited_once_with('1', 'Example Two')
    from nonebot.adapters.onebot.v11 import Message
    bot.send.assert_awaited_once_with(event, Message('image'))
    event.group_id = 2
    asyncio.run(commands.handle_mentioned_game(bot, event))
    assert query.await_count == 1


def test_group_lock_released_after_send_failure(monkeypatch):
    nonebot.init()
    from qq_bot import game_commands as commands
    from nonebot.adapters.onebot.v11 import Message
    monkeypatch.setattr(commands, 'config', SimpleNamespace(allowed_groups=set()))
    monkeypatch.setattr(commands, 'progress_service', SimpleNamespace(query=AsyncMock(return_value=None)))
    async def messages(_):
        yield 'image'
    monkeypatch.setattr(commands, 'build_progress_messages', messages)
    bot = SimpleNamespace(send=AsyncMock(side_effect=RuntimeError('delivery')))
    with pytest.raises(RuntimeError, match='delivery'):
        asyncio.run(commands.handle_game_command(bot, SimpleNamespace(group_id=1), Message('10')))
    assert not commands._progress_groups
    assert bot.send.await_count == 1


def test_cached_assets_used_offline_across_pages(monkeypatch, tmp_path):
    from qq_bot import list_cards, game_progress
    monkeypatch.setattr(list_cards, 'CACHE_DIR', tmp_path)
    content = BytesIO()
    Image.new('RGB', (10, 10), '#123456').save(content, 'PNG')
    avatar, cover = 'https://example.com/avatar', 'https://example.com/cover'
    for url in (avatar, cover):
        list_cards._cache_path(url).write_bytes(content.getvalue())
    download = AsyncMock(side_effect=RuntimeError('offline'))
    monkeypatch.setattr(list_cards, '_download_image', download)
    players = [PlayerProgress(str(i), str(i), avatar, 1, 2, None, None) for i in range(13)]
    seen = []
    def render(result, rows, assets, page, pages):
        seen.append(dict(assets))
        return content.getvalue()
    monkeypatch.setattr(game_progress, 'render_progress', render)
    async def collect():
        return [v async for v in build_progress_messages(GameProgress('10', 'Game', cover, players))]
    assert len(asyncio.run(collect())) == 2
    assert all(assets == {avatar: content.getvalue(), cover: content.getvalue()} for assets in seen)
    download.assert_not_awaited()


def test_all_pages_sent_in_one_message(monkeypatch):
    nonebot.init()
    from qq_bot import game_commands as commands
    from nonebot.adapters.onebot.v11 import Message, MessageSegment
    monkeypatch.setattr(commands, 'config', SimpleNamespace(allowed_groups=set()))
    monkeypatch.setattr(commands, 'progress_service', SimpleNamespace(query=AsyncMock(return_value=None)))
    pages = [MessageSegment.image(b'page1'), MessageSegment.image(b'page2')]
    async def messages(_):
        for page in pages:
            yield Message(page)
    monkeypatch.setattr(commands, 'build_progress_messages', messages)
    bot = SimpleNamespace(send=AsyncMock())
    event = SimpleNamespace(group_id=1)
    asyncio.run(commands.handle_game_command(bot, event, Message('10')))
    bot.send.assert_awaited_once()
    assert list(bot.send.await_args.args[1]) == pages


@pytest.mark.parametrize('broken_cache', [False, True])
def test_missing_or_invalid_avatar_cache_is_refilled(monkeypatch, tmp_path, broken_cache):
    from qq_bot import list_cards, game_progress
    monkeypatch.setattr(list_cards, 'CACHE_DIR', tmp_path)
    avatar = 'https://example.com/avatar'
    if broken_cache:
        list_cards._cache_path(avatar).write_bytes(b'invalid image')
    content = BytesIO()
    Image.new('RGB', (10, 10), '#123456').save(content, 'PNG')
    download = AsyncMock(return_value=content.getvalue())
    monkeypatch.setattr(list_cards, '_download_image', download)
    monkeypatch.setattr(game_progress, 'render_progress', lambda *args: content.getvalue())
    players = [PlayerProgress(str(i), str(i), avatar, 1, 2, None, None) for i in range(13)]
    async def collect():
        return [v async for v in build_progress_messages(GameProgress('10', 'Game', None, players))]
    asyncio.run(collect())
    asyncio.run(collect())
    download.assert_awaited_once_with(avatar)
    assert list_cards._cache_path(avatar).read_bytes() == content.getvalue()

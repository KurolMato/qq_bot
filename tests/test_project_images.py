import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import nonebot
import pytest

from qq_bot.project_images import ImageRequest, image_request


def action_text(data):
    return "[[PROJECT_IMAGE:" + json.dumps(data, ensure_ascii=False) + "]]"


@pytest.mark.parametrize(("data", "expected"), [
    ({"action": "online"}, ImageRequest("game", "ol")),
    ({"action": "rank"}, ImageRequest("rank", "")),
    ({"action": "rank", "period": "week"}, ImageRequest("rank", "w")),
    ({"action": "rank", "period": "yesterday", "scope": "me"}, ImageRequest("rank", "me y")),
    ({"action": "rank", "period": "month", "month": 9, "board": "other"}, ImageRequest("rank", "o m 9")),
    ({"action": "hot"}, ImageRequest("hot", "")),
    ({"action": "hot", "period": "month"}, ImageRequest("hot", "m")),
    ({"action": "profile", "month": 9}, ImageRequest("rank", "me 9")),
    ({"action": "players", "platform": "psn"}, ImageRequest("psn", "list")),
    ({"action": "calendar"}, ImageRequest("game", "list")),
    ({"action": "calendar", "selector": "2027-9"}, ImageRequest("game", "list 2027-9")),
    ({"action": "calendar", "selector": "2027"}, ImageRequest("game", "list 2027")),
    ({"action": "progress", "game": "Elden Ring"}, ImageRequest("game", "Elden Ring")),
    ({"action": "help"}, ImageRequest("help")),
])
def test_valid_model_actions(data, expected):
    from qq_bot.project_images import model_image_request
    assert model_image_request(action_text(data)) == expected


@pytest.mark.parametrize("data", [
    {"action": "shell", "command": "anything"},
    {"action": "online", "group_id": "999"},
    {"action": "rank", "argument": "remove game"},
    {"action": "rank", "period": "last_week"},
    {"action": "rank", "scope": "another_user"},
    {"action": "rank", "scope": "me", "board": "other"},
    {"action": "rank", "month": 9},
    {"action": "hot", "period": "last_month"},
    {"action": "hot", "period": "month", "group_id": "999"},
    {"action": "profile", "month": True},
    {"action": "profile", "month": 13},
    {"action": "players", "platform": "steam remove 123"},
    {"action": "calendar", "selector": "2027-13"},
    {"action": "calendar", "selector": "add game"},
    {"action": "progress", "game": "add game"},
    {"action": "progress", "game": "LIST 2027"},
    {"action": "progress", "game": ""},
    {"action": "progress", "game": "game\nadd game"},
    {"action": "progress", "game": []},
    {"action": []},
])
def test_invalid_model_actions_are_rejected(data):
    from qq_bot.project_images import model_image_request
    with pytest.raises(ValueError):
        model_image_request(action_text(data))


@pytest.mark.parametrize("text", ["[[PROJECT_IMAGE:{", '先发图' + action_text({"action": "online"}),
    action_text({"action": "online"}) * 2])
def test_model_action_must_be_one_complete_envelope(text):
    from qq_bot.project_images import model_image_request
    with pytest.raises(ValueError):
        model_image_request(text)


@pytest.mark.parametrize(("cancelled", "stop"), [(True, "cancelled"), (False, "timeout"), (False, "error")])
def test_incomplete_model_turn_never_dispatches(monkeypatch, cancelled, stop):
    from qq_bot import project_images
    from qq_bot.dsh_client import DshAnswer
    send = AsyncMock()
    monkeypatch.setattr(project_images, "send_project_image", send)
    bot = SimpleNamespace(send=AsyncMock())
    answer = DshAnswer(action_text({"action": "online"}), "sid", 1, cancelled=cancelled, stop_reason=stop)
    assert asyncio.run(project_images.deliver_model_image(bot, object(), answer))
    send.assert_not_awaited()
    assert "PROJECT_IMAGE" not in bot.send.await_args.args[1]


@pytest.mark.parametrize(("text", "kind", "argument"), [
    ("排行", "rank", ""), ("rank", "rank", ""), ("周榜", "rank", "w"),
    ("昨日排行", "rank", "y"), ("我的排名", "rank", "me"),
    ("我的今日排行", "rank", "me d"), ("我的周榜", "rank", "me w"),
    ("9月排行", "rank", "m 9"), ("另一组排行", "rank", "o"),
    ("我的9月档案", "rank", "me 9"), ("另一组周榜", "rank", "o w"),
    ("/hot", "hot", ""), ("/hot m", "hot", "m"),
    ("本周热游", "hot", ""), ("帮我看看这个月群热游榜", "hot", "m"),
    ("在线", "game", "ol"), ("谁在线", "game", "ol"),
    ("Steam列表", "steam", "list"), ("PS5玩家列表", "psn", "list"),
    ("switch 列表", "switch", "list"), ("Xbox名单", "xbox", "list"),
    ("发售日历", "game", "list"), ("发售日历 2027-9", "game", "list 2027-9"),
    ("9月发售日历", "game", "list 9"), ("发售日历 2026", "game", "list 2026"),
    ("游戏进度 黑神话悟空", "game", "黑神话悟空"),
    ("成就进度 Elden Ring", "game", "Elden Ring"),
    ("指令图", "help", ""), ("帮我看看在线列表", "game", "ol"),
    ("发一下Steam列表", "steam", "list"), ("个人档案", "rank", "me"),
])
def test_image_request(text, kind, argument):
    assert image_request(text) == ImageRequest(kind, argument)


@pytest.mark.parametrize("text", [
    "不要发Steam列表", "Steam列表怎么用", "排行榜不错", "游戏进度 add something",
    "游戏进度 list", "steam remove 123", "rank remove 游戏", "发售日历 add", "普通聊天",
    "别发本月热游", "/hot m 怎么用", "/hot m 9", "上月热游",
])
def test_not_a_read_only_image_request(text):
    assert image_request(text) is None


@pytest.mark.parametrize(("kind", "module", "name", "argument"), [
    ("rank", "game_time_commands", "handle_rank_command", ""),
    ("hot", "game_time_commands", "handle_hot_command", "m"),
    ("game", "game_commands", "handle_game_command", "ol"),
    ("steam", "steam_commands", "handle_steam_command", "list"),
    ("psn", "ps5_commands", "handle_ps5_command", "list"),
    ("switch", "switch_commands", "handle_switch_command", "list"),
    ("xbox", "xbox_commands", "handle_xbox_command", "list"),
    ("help", "help_message", "send_help", ""),
])
def test_dispatch_reuses_existing_handlers(monkeypatch, kind, module, name, argument):
    from qq_bot import project_images
    handler = AsyncMock()
    def load(path, package):
        assert path == f".{module}"
        return SimpleNamespace(**{name: handler})
    monkeypatch.setattr(project_images.importlib, "import_module", load)
    bot, event = object(), object()
    asyncio.run(project_images.send_project_image(bot, event, ImageRequest(kind, argument)))
    args = handler.await_args.args
    assert args[:2] == (bot, event)
    if kind != "help":
        assert args[2].extract_plain_text() == argument


@pytest.mark.parametrize(("groups", "text", "expected"), [
    ("123", "排行", "123"), ("123,456", "排行", None),
    ("123,456", "群号 456 在线", "456"), ("123", "群号 999 排行", None),
    ("123,456", "群号 456 /hot m", "456"), ("123", "群号 999 /hot", None),
])
def test_private_scope_and_delivery(monkeypatch, groups, text, expected):
    nonebot.init()
    from qq_bot import project_images
    monkeypatch.setenv("QQ_DSH_GROUPS", groups)
    monkeypatch.setenv("QQ_ALLOWED_GROUPS", groups)
    original = SimpleNamespace(user_id=7)
    bot = SimpleNamespace(send=AsyncMock())
    seen = []
    async def send(proxy, scoped, request):
        seen.append(str(scoped.group_id))
        assert scoped.user_id == original.user_id
        await proxy.send(scoped, "image")
    monkeypatch.setattr(project_images, "send_project_image", send)
    assert asyncio.run(project_images.private_image_request(bot, original, text))
    assert seen == ([expected] if expected else [])
    assert bot.send.await_args.args[0] is original
    if expected:
        bot.send.assert_awaited_once_with(original, "image")

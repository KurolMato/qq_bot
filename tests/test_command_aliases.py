import asyncio
from types import SimpleNamespace

import nonebot
import pytest
from nonebot.adapters.onebot.v11 import Message


def _command_names(matcher) -> set[str]:
    names: set[str] = set()
    for dependency in matcher.rule.checkers:
        command = getattr(dependency, "call", None)
        for item in getattr(command, "cmds", ()):
            if item:
                names.add(item[0])
    return names


def test_rank_aliases_are_registered():
    nonebot.init()
    from qq_bot import game_time_commands as commands

    names = _command_names(commands.rank_command)
    assert {"rank", "排行", "游戏排行", "今日排行", "排", "排名", "排行榜"} <= names


def test_rank_me_aliases_are_registered():
    nonebot.init()
    from qq_bot import game_time_commands as commands

    names = _command_names(commands.rank_me_command)
    assert {"我的排名", "排名我", "我的排行"} <= names


def test_online_aliases_are_registered():
    nonebot.init()
    from qq_bot import game_commands as commands

    names = _command_names(commands.online_command)
    assert {"在线", "谁在线", "在线玩家", "在线列表"} <= names


@pytest.mark.parametrize(
    ("argument", "expected"),
    [("", "me"), ("9", "me 9"), ("w", "me w")],
)
def test_rank_me_alias_dispatches_to_rank_me(monkeypatch, argument, expected):
    nonebot.init()
    from qq_bot import game_time_commands as commands

    calls: list[str] = []

    async def fake_rank(bot, event, message):
        calls.append(message.extract_plain_text())

    monkeypatch.setattr(commands, "handle_rank_command", fake_rank)
    event = SimpleNamespace(group_id=1, user_id=2)
    asyncio.run(commands.handle_rank_me_command(SimpleNamespace(), event, Message(argument)))
    assert calls == [expected]


def test_online_alias_dispatches_to_game_ol(monkeypatch):
    nonebot.init()
    from qq_bot import game_commands as commands

    calls: list[str] = []

    async def fake_game(bot, event, message):
        calls.append(message.extract_plain_text())

    monkeypatch.setattr(commands, "handle_game_command", fake_game)
    event = SimpleNamespace(group_id=1, user_id=2)
    asyncio.run(commands.handle_online_command(SimpleNamespace(), event))
    assert calls == ["ol"]

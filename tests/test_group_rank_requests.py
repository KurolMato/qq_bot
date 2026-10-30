import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import nonebot
import pytest
from nonebot.adapters.onebot.v11 import Message, MessageSegment

from qq_bot.group_rules import rank_request


@pytest.mark.parametrize(("text", "argument"), [
    ("您你能不能把今天的排行榜发一下？", ""),
    ("发一下今天的排行榜", ""),
    ("给我看看排行榜", ""),
    ("昨天的排行发一下", "y"),
    ("看看本周排行榜", "w"),
    ("来一张月榜", "m"),
    ("把我的排名发一下", "me"),
    ("查一下我今天的排行", "me d"),
    ("查一下我的今日排行", "me d"),
    ("发一下我的本周排行", "me w"),
])
def test_rank_request(text, argument):
    assert rank_request(text) == argument


@pytest.mark.parametrize("text", [
    "今天的天气怎么样", "排行榜怎么发", "不要发排行榜", "别发排行榜",
    "为什么排行榜没发出来", "排行榜的代码给我看一下", "发上周排行榜",
    "发9月排行榜", "发今天和昨天的排行榜", "排行榜不错", "把别的群排行榜发来",
    "删除排行榜里某个游戏",
])
def test_unrelated_or_unsupported_request_stays_chat(text):
    assert rank_request(text) is None


@pytest.fixture
def bridge(monkeypatch):
    nonebot.init()
    from qq_bot import group_dsh, game_time_commands
    monkeypatch.setenv("QQ_DSH_GROUP_ENABLED", "true")
    monkeypatch.setenv("QQ_DSH_GROUPS", "123")
    monkeypatch.setattr(group_dsh, "_locks", {})
    monkeypatch.setattr(game_time_commands, "config", SimpleNamespace(allowed_groups={"123"}))
    return group_dsh, game_time_commands


def make_event(text, *, mention="10000", group_id=123):
    original = MessageSegment.at(mention) + Message(text)
    return SimpleNamespace(group_id=group_id, user_id=456, self_id=10000,
                           original_message=original, raw_message=str(original),
                           get_plaintext=lambda: text)


@pytest.mark.parametrize("text", ["您你能不能把今天的排行榜发一下？", "排行", "rank"])
def test_mention_sends_existing_rank_image(bridge, monkeypatch, text):
    group, ranks = bridge
    seen = []
    snapshot = SimpleNamespace(members=[object()])
    def fake_snapshot(group_id, **kwargs):
        seen.append((group_id, kwargs))
        return snapshot
    monkeypatch.setattr(ranks, "tracker", SimpleNamespace(snapshot=fake_snapshot))
    image = Message(MessageSegment.image("file:///test-rank.png"))
    build = AsyncMock(return_value=image)
    monkeypatch.setattr(ranks, "build_leaderboard_message", build)
    bot = SimpleNamespace(self_id=10000, send=AsyncMock())
    event = make_event(text)
    asyncio.run(group.handle_group_question(bot, event))
    assert seen == [("123", {"board": "main"})]
    build.assert_awaited_once_with(snapshot)
    bot.send.assert_awaited_once_with(event, image)


@pytest.mark.parametrize(("mention", "group_id", "enabled"), [
    ("99999", 123, "true"), ("10000", 999, "true"), ("10000", 123, "false"),
])
def test_rank_requires_bot_mention_and_allowed_enabled_group(bridge, monkeypatch, mention, group_id, enabled):
    group, ranks = bridge
    monkeypatch.setenv("QQ_DSH_GROUP_ENABLED", enabled)
    handler = AsyncMock()
    monkeypatch.setattr(ranks, "handle_rank_command", handler)
    # Non-mentions still reach the existing style recorder, with no real file writes.
    from qq_bot import dsh_commands
    monkeypatch.setattr(dsh_commands, "_record_style", AsyncMock())
    bot = SimpleNamespace(self_id=10000, send=AsyncMock())
    asyncio.run(group.handle_group_question(bot, make_event("发今天的排行榜", mention=mention, group_id=group_id)))
    handler.assert_not_awaited()
    bot.send.assert_not_awaited()

import asyncio

import nonebot
from nonebot.adapters.onebot.v11 import GroupMessageEvent
from nonebot.adapters.onebot.v11.bot import _check_at_me


nonebot.init()

from qq_bot.help_message import _is_direct_mention_without_command  # noqa: E402


class FakeBot:
    self_id = "100000001"


def _event(text: str, *, reply: bool = False) -> GroupMessageEvent:
    message = []
    if reply:
        message.append({"type": "reply", "data": {"id": "9"}})
    message.extend([
        {"type": "at", "data": {"qq": "100000001"}},
        {"type": "text", "data": {"text": text}},
    ])
    return GroupMessageEvent.model_validate({
        "time": 1,
        "self_id": 100000001,
        "post_type": "message",
        "message_type": "group",
        "sub_type": "normal",
        "message_id": 10,
        "group_id": 100,
        "user_id": 200,
        "message": message,
        "raw_message": "",
        "font": 0,
        "sender": {"user_id": 200, "nickname": "tester", "role": "member"},
    })


def test_help_mention_survives_onebot_at_preprocessing() -> None:
    event = _event(" ")
    _check_at_me(FakeBot(), event)  # type: ignore[arg-type]

    assert event.to_me is True
    assert all(segment.type != "at" for segment in event.message)
    assert any(segment.type == "at" for segment in event.original_message)
    assert asyncio.run(_is_direct_mention_without_command(FakeBot(), event)) is True  # type: ignore[arg-type]


def test_mention_with_command_does_not_open_help() -> None:
    event = _event(" /switch list")
    _check_at_me(FakeBot(), event)  # type: ignore[arg-type]

    assert asyncio.run(_is_direct_mention_without_command(FakeBot(), event)) is False  # type: ignore[arg-type]


def test_replying_to_bot_card_does_not_open_help() -> None:
    event = _event(" ", reply=True)
    _check_at_me(FakeBot(), event)  # type: ignore[arg-type]

    assert event.to_me is True
    assert any(segment.type == "reply" for segment in event.original_message)
    assert asyncio.run(_is_direct_mention_without_command(FakeBot(), event)) is False  # type: ignore[arg-type]


def test_mention_with_plain_text_does_not_open_help() -> None:
    event = _event(" 你好")
    _check_at_me(FakeBot(), event)  # type: ignore[arg-type]

    assert asyncio.run(_is_direct_mention_without_command(FakeBot(), event)) is False  # type: ignore[arg-type]

import asyncio
import base64
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import nonebot
import pytest
from PIL import Image
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


def test_public_installation_without_optional_poster_returns_text(monkeypatch, tmp_path):
    from qq_bot import help_message as help_module
    monkeypatch.setattr(help_module, "HELP_IMAGE", tmp_path / "missing.png")
    monkeypatch.setattr(help_module, "LOCAL_HELP_IMAGE", tmp_path / "missing.local.png")
    monkeypatch.setattr(help_module, "config", SimpleNamespace(allowed_groups=frozenset()))
    bot = SimpleNamespace(send=AsyncMock())
    incoming = _event(" ")
    asyncio.run(help_module.send_help(bot, incoming))
    bot.send.assert_awaited_once_with(incoming, help_module.HELP_TEXT)


def test_release_contains_readable_default_instruction_poster():
    image = Path(__file__).resolve().parents[1] / "assets/instruction.png"
    assert image.is_file(), "The default instruction poster must ship in releases"
    with Image.open(image) as poster:
        assert poster.format == "PNG"
        assert poster.width >= 1000 and poster.height >= 600
        poster.verify()


@pytest.mark.parametrize("custom", [False, True])
def test_help_sends_default_image_or_local_override(monkeypatch, tmp_path, custom):
    from qq_bot import help_message as help_module
    public = tmp_path / "instruction.png"
    private = tmp_path / "instruction.local.png"
    public.write_bytes(b"public image")
    if custom:
        private.write_bytes(b"custom image")
    monkeypatch.setattr(help_module, "HELP_IMAGE", public)
    monkeypatch.setattr(help_module, "LOCAL_HELP_IMAGE", private)
    monkeypatch.setattr(help_module, "config", SimpleNamespace(allowed_groups=frozenset()))
    bot = SimpleNamespace(send=AsyncMock())
    incoming = _event(" ")
    asyncio.run(help_module.send_help(bot, incoming))
    segment = bot.send.await_args.args[1]
    assert segment.type == "image"
    assert base64.b64decode(segment.data["file"].removeprefix("base64://")) == (
        b"custom image" if custom else b"public image"
    )

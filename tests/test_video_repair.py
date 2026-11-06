from __future__ import annotations

import asyncio

import pytest

from qq_bot import video_repair
from qq_bot.service import CompletedVideo
from qq_bot.video_repair import parse_delays, repair_key, schedule_repair


def test_repair_stops_after_send_timeout_and_blocks_parallel_handler(monkeypatch):
    from nonebot.adapters.onebot.v11.exception import NetworkError
    from qq_bot import plugin
    from unittest.mock import AsyncMock
    from types import SimpleNamespace
    monkeypatch.setenv('QQ_REPAIR_DELAYS', '0,0,0')
    monkeypatch.setenv('QQ_REPAIR_ENABLED', 'true')
    monkeypatch.setenv('QQ_REPAIR_NOTE', '')
    monkeypatch.setattr(video_repair, '_schedule_cleanup', lambda *args: None)
    bot = FakeBot()
    bot.send = AsyncMock()
    source = 'https://v.douyin.com/uncertain/'
    async def send(**kwargs):
        bot.group_messages.append(kwargs)
        raise NetworkError('WebSocket timeout')
    bot.send_group_msg = send
    service = FakeService()
    monkeypatch.setattr(plugin, 'extract_video_urls', lambda payload: [source])
    monkeypatch.setattr(plugin, 'config', SimpleNamespace(allowed_groups=frozenset()))
    foreground = AsyncMock()
    monkeypatch.setattr(plugin, '_acquire_shared_video', foreground)
    event = SimpleNamespace(group_id=123, message_id=1, get_message=lambda: [])
    async def run():
        schedule_repair(bot, '123', source, 'download failed', service)
        await plugin.handle_video_link(bot, event)
        await asyncio.gather(*list(video_repair._tasks))
        await plugin.handle_video_link(bot, event)
    asyncio.run(run())
    assert service.calls == 1
    assert len(bot.group_messages) == 1
    foreground.assert_not_awaited()


class FakeBot:
    self_id = "10000"

    def __init__(self) -> None:
        self.group_messages: list[dict] = []

    async def send_group_msg(self, **kwargs) -> None:
        self.group_messages.append(kwargs)

    async def send_group_forward_msg(self, **kwargs) -> None:
        self.group_messages.append(kwargs)


class FakeService:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.calls = 0

    async def convert(self, url: str) -> CompletedVideo:
        self.calls += 1
        if not self.ok:
            raise RuntimeError("still broken")
        return CompletedVideo(job_id="job-1", url="http://api/video.mp4", filename="v.mp4", size_bytes=1024)

    async def delete(self, job_id: str) -> None:
        return None


def test_parse_delays_handles_noise() -> None:
    assert parse_delays("45, 180 ,600") == [45.0, 180.0, 600.0]
    assert parse_delays("bad,,10") == [10.0]
    assert parse_delays("") == [45.0, 180.0, 600.0]


def test_repair_key_is_scoped_by_group() -> None:
    assert repair_key("1", "u") != repair_key("2", "u")


def test_repair_resends_after_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QQ_REPAIR_ENABLED", "true")
    monkeypatch.setenv("QQ_REPAIR_DELAYS", "0")
    monkeypatch.setenv("QQ_REPAIR_NOTE", "补发：")
    monkeypatch.setenv("QQ_REPAIR_DIAG", "false")
    bot = FakeBot()
    service = FakeService()

    async def run() -> None:
        schedule_repair(bot, "123", "https://v.douyin.com/x", "boom", service)  # type: ignore[arg-type]
        await asyncio.gather(*list(video_repair._tasks))

    asyncio.run(run())
    assert service.calls == 1
    assert bot.group_messages, "补发没有发到群里"
    assert bot.group_messages[0]["message"] == "补发："


def test_repair_keeps_trying_until_it_gives_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QQ_REPAIR_ENABLED", "true")
    monkeypatch.setenv("QQ_REPAIR_DELAYS", "0,0")
    monkeypatch.setenv("QQ_REPAIR_DIAG", "false")
    bot = FakeBot()
    service = FakeService(ok=False)

    async def run() -> None:
        schedule_repair(bot, "123", "https://v.douyin.com/y", "boom", service)  # type: ignore[arg-type]
        await asyncio.gather(*list(video_repair._tasks))

    asyncio.run(run())
    assert service.calls == 2
    assert bot.group_messages == []


def test_non_repairable_reason_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QQ_REPAIR_ENABLED", "true")
    bot = FakeBot()
    service = FakeService(ok=False)

    async def run() -> None:
        schedule_repair(bot, "123", "https://v.douyin.com/z", "NO_VIDEO", service)  # type: ignore[arg-type]
        await asyncio.gather(*list(video_repair._tasks))

    asyncio.run(run())
    assert service.calls == 0
    assert bot.group_messages == []

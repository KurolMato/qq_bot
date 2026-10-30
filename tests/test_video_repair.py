from __future__ import annotations

import asyncio

import pytest

from qq_bot import video_repair
from qq_bot.service import CompletedVideo
from qq_bot.video_repair import parse_delays, repair_key, schedule_repair


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
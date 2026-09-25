import asyncio
import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from qq_bot import plugin
from qq_bot.service import CompletedVideo, JobFailed


VIDEO_URL = "https://x.com/example/status/1234567890"


class _Segment:
    data = VIDEO_URL


class _Event:
    def __init__(self, group_id: int) -> None:
        self.group_id = group_id
        self.message_id = group_id

    def get_message(self) -> list[_Segment]:
        return [_Segment()]


class _Bot:
    def __init__(self) -> None:
        self.sent_groups: list[int] = []
        self.replies: list[object] = []

    async def send_group_msg(self, *, group_id: int, message: object) -> None:
        self.sent_groups.append(group_id)

    async def send(self, _event: object, message: object) -> None:
        self.replies.append(message)


def _reset_plugin_state() -> None:
    plugin._recent.clear()
    plugin._shared_videos.clear()


def test_failed_conversion_releases_group_deduplication(monkeypatch) -> None:
    _reset_plugin_state()
    convert = AsyncMock(side_effect=JobFailed("temporary failure"))
    monkeypatch.setattr(plugin, "service", SimpleNamespace(convert=convert))
    monkeypatch.setattr(plugin, "config", SimpleNamespace(allowed_groups=frozenset()))

    async def run() -> None:
        await plugin.handle_video_link(_Bot(), _Event(100))
        assert await plugin._is_duplicate("100", VIDEO_URL) is False

    asyncio.run(run())
    assert convert.await_count == 1
    _reset_plugin_state()


def test_cancelled_group_does_not_cancel_another_groups_conversion(monkeypatch) -> None:
    _reset_plugin_state()
    monkeypatch.setattr(plugin, "config", SimpleNamespace(allowed_groups=frozenset()))
    monkeypatch.setattr(plugin, "VIDEO_CLEANUP_DELAY_SECONDS", 0.0)

    async def run() -> None:
        ready = asyncio.Event()
        video = CompletedVideo("job-cancel", "http://localhost/video", "video.mp4", 123)

        async def convert_url(_url):
            await ready.wait()
            return video

        convert = AsyncMock(side_effect=convert_url)
        delete = AsyncMock()
        monkeypatch.setattr(plugin, "service", SimpleNamespace(convert=convert, delete=delete))
        bot = _Bot()
        first = asyncio.create_task(plugin.handle_video_link(bot, _Event(100)))
        second = asyncio.create_task(plugin.handle_video_link(bot, _Event(200)))
        await asyncio.sleep(0)
        shared = plugin._shared_videos[VIDEO_URL]
        assert shared.leases == 2
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert not shared.task.cancelled()
        assert plugin._shared_videos[VIDEO_URL] is shared
        assert shared.leases == 1
        assert ("100", VIDEO_URL) not in plugin._recent
        retry = asyncio.create_task(plugin.handle_video_link(bot, _Event(100)))
        await asyncio.sleep(0)
        assert shared.leases == 2
        ready.set()
        await asyncio.gather(second, retry)
        if shared.cleanup_task is not None:
            await shared.cleanup_task
        assert sorted(bot.sent_groups) == [100, 200]
        assert convert.await_count == 1
        delete.assert_awaited_once_with(video.job_id)
        assert not plugin._shared_videos

    asyncio.run(run())
    _reset_plugin_state()


def test_completed_shared_video_reuse_restarts_cleanup_delay(monkeypatch) -> None:
    _reset_plugin_state()
    monkeypatch.setattr(plugin, "VIDEO_CLEANUP_DELAY_SECONDS", 0.01)
    video = CompletedVideo("job-reuse", "http://localhost/video", "video.mp4", 123)
    convert = AsyncMock(return_value=video)
    delete = AsyncMock()
    monkeypatch.setattr(plugin, "service", SimpleNamespace(convert=convert, delete=delete))

    async def run() -> None:
        shared, _ = await plugin._acquire_shared_video(VIDEO_URL)
        await plugin._release_shared_video(VIDEO_URL, shared)
        old_cleanup = shared.cleanup_task
        again, result = await plugin._acquire_shared_video(VIDEO_URL)
        assert again is shared
        assert result is video
        await asyncio.sleep(0.02)
        assert old_cleanup.cancelled()
        delete.assert_not_awaited()
        await plugin._release_shared_video(VIDEO_URL, again)
        await shared.cleanup_task
        delete.assert_awaited_once_with(video.job_id)
        assert convert.await_count == 1
        assert not plugin._shared_videos

    asyncio.run(run())
    _reset_plugin_state()


@pytest.mark.parametrize("fails", [False, True])
def test_all_cancelled_waiters_finish_and_clean_orphan_conversion(monkeypatch, fails) -> None:
    _reset_plugin_state()
    monkeypatch.setattr(plugin, "VIDEO_CLEANUP_DELAY_SECONDS", 0.01)

    async def run() -> None:
        ready = asyncio.Event()
        finished = asyncio.Event()
        video = CompletedVideo("job-orphan", "http://localhost/video", "video.mp4", 123)

        async def convert_url(_url):
            await ready.wait()
            if fails:
                raise JobFailed("orphan conversion failed")
            return video

        delete = AsyncMock()
        monkeypatch.setattr(plugin, "service", SimpleNamespace(convert=AsyncMock(side_effect=convert_url), delete=delete))
        first = asyncio.create_task(plugin._acquire_shared_video(VIDEO_URL))
        second = asyncio.create_task(plugin._acquire_shared_video(VIDEO_URL))
        await asyncio.sleep(0)
        shared = plugin._shared_videos[VIDEO_URL]
        first.cancel()
        second.cancel()
        await asyncio.gather(first, second, return_exceptions=True)
        assert shared.leases == 0
        assert not shared.task.cancelled()
        shared.task.add_done_callback(lambda _task: finished.set())
        ready.set()
        await finished.wait()
        if fails:
            # The orphan's exception must have been retrieved by its callback,
            # without any caller awaiting the conversion task.
            assert shared.task._log_traceback is False
            delete.assert_not_awaited()
        else:
            delete.assert_not_awaited()
            assert shared.cleanup_task is not None
            await shared.cleanup_task
            delete.assert_awaited_once_with(video.job_id)
        assert not plugin._shared_videos

    asyncio.run(run())
    _reset_plugin_state()


def test_same_url_in_two_groups_shares_one_conversion_and_cleans_later(monkeypatch) -> None:
    _reset_plugin_state()
    video = CompletedVideo(
        job_id="job-1",
        url="http://127.0.0.1:8000/api/jobs/job-1/file",
        filename="video.mp4",
        size_bytes=123,
    )
    convert = AsyncMock(return_value=video)
    delete = AsyncMock()
    monkeypatch.setattr(plugin, "service", SimpleNamespace(convert=convert, delete=delete))
    monkeypatch.setattr(plugin, "config", SimpleNamespace(allowed_groups=frozenset()))
    monkeypatch.setattr(plugin, "VIDEO_CLEANUP_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(plugin, "VIDEO_CLEANUP_RETRY_SECONDS", 0.01)
    bot = _Bot()

    async def run() -> None:
        await asyncio.gather(
            plugin.handle_video_link(bot, _Event(100)),
            plugin.handle_video_link(bot, _Event(200)),
        )
        for _ in range(20):
            if delete.await_count:
                break
            await asyncio.sleep(0.01)

    asyncio.run(run())

    assert convert.await_count == 1
    assert sorted(bot.sent_groups) == [100, 200]
    assert delete.await_count == 1
    assert not plugin._shared_videos
    _reset_plugin_state()

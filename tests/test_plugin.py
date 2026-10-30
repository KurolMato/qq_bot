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
    self_id = "123456789"

    def __init__(self) -> None:
        self.sent_groups: list[int] = []
        self.replies: list[object] = []
        self.messages: list[object] = []
        self.forwarded: list[dict] = []

    async def send_group_forward_msg(self, **kwargs):
        self.forwarded.append(kwargs)

    async def send_group_msg(self, *, group_id: int, message: object) -> None:
        self.sent_groups.append(group_id)
        self.messages.append(message)

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


def test_image_post_sends_only_images_in_order_then_cleans(monkeypatch) -> None:
    _reset_plugin_state()
    urls = tuple(f"http://127.0.0.1:8000/api/jobs/pictures/images/{i}" for i in range(12))
    result = CompletedVideo("pictures", "", "", 0, image_urls=urls)
    convert, delete = AsyncMock(return_value=result), AsyncMock()
    monkeypatch.setattr(plugin, "service", SimpleNamespace(convert=convert, delete=delete))
    monkeypatch.setattr(plugin, "config", SimpleNamespace(allowed_groups=frozenset()))
    monkeypatch.setattr(plugin, "VIDEO_CLEANUP_DELAY_SECONDS", 0.0)
    bot = _Bot()

    async def run():
        await plugin.handle_video_link(bot, _Event(100))
        shared = plugin._shared_videos[VIDEO_URL]
        await shared.cleanup_task

    asyncio.run(run())
    assert [len(message) for message in bot.messages] == [10, 2]
    assert [segment.type for message in bot.messages for segment in message] == ["image"] * 12
    assert [segment.data["file"] for message in bot.messages for segment in message] == list(urls)
    delete.assert_awaited_once_with("pictures")
    assert bot.replies == []
    _reset_plugin_state()


@pytest.mark.parametrize("source", ["https://www.xiaoheihe.cn/app/bbs/link/5a28725c3264", "https://www.bilibili.com/opus/917675369769730096", "https://www.douyin.com/note/7351234567890123456"])
@pytest.mark.parametrize("count", [1, 4, 5, 6, 12])
@pytest.mark.parametrize("title", ["", "图文标题 [CQ:at,qq=all]"])
def test_image_forward_threshold_order_and_cleanup(monkeypatch, count, source, title):
    _reset_plugin_state()
    urls = tuple(f"http://127.0.0.1:8000/api/jobs/pictures/images/{i}" for i in range(count))
    convert = AsyncMock(return_value=CompletedVideo("pictures", "", "", 0, image_urls=urls, title=title))
    delete = AsyncMock()
    monkeypatch.setattr(plugin, "extract_video_urls", lambda payload: [source])
    monkeypatch.setattr(plugin, "service", SimpleNamespace(convert=convert, delete=delete))
    monkeypatch.setattr(plugin, "config", SimpleNamespace(allowed_groups=frozenset()))
    monkeypatch.setattr(plugin, "VIDEO_CLEANUP_DELAY_SECONDS", 0.0)
    bot = _Bot()
    async def run():
        await plugin.handle_video_link(bot, _Event(100))
        shared = plugin._shared_videos[source]
        await shared.cleanup_task
    asyncio.run(run())
    is_douyin = "douyin.com" in source
    sent_title = title if not is_douyin else ""
    if count >= (5 if is_douyin else 6):
        assert bot.messages == []
        assert len(bot.forwarded) == 1
        assert bot.forwarded[0]["group_id"] == 100
        nodes = bot.forwarded[0]["messages"]
        assert len(nodes) == count + bool(sent_title)
        assert all(node.type == "node" and node.data["user_id"] == bot.self_id for node in nodes)
        if is_douyin:
            assert all(node.data["nickname"] == "抖音图文" for node in nodes)
        if sent_title:
            assert nodes[0].data["content"][0].type == "text"
            assert nodes[0].data["content"][0].data["text"] == sent_title
            nodes = nodes[1:]
        assert [node.data["content"][0].data["file"] for node in nodes] == list(urls)
    else:
        assert bot.forwarded == []
        assert len(bot.messages) == 1
        message = bot.messages[0]
        if sent_title:
            assert message[0].type == "text"
            assert message[0].data["text"] == sent_title + "\n"
            message = message[1:]
        assert [segment.data["file"] for segment in message] == list(urls)
    assert bot.replies == []
    delete.assert_awaited_once_with("pictures")
    _reset_plugin_state()


def test_xiaoheihe_failed_forward_releases_dedup_and_shared_lease(monkeypatch):
    _reset_plugin_state()
    source = "https://www.xiaoheihe.cn/app/bbs/link/5a28725c3264"
    result = CompletedVideo("pictures", "", "", 0, image_urls=tuple(f"http://localhost/{i}" for i in range(6)))
    monkeypatch.setattr(plugin, "extract_video_urls", lambda payload: [source])
    monkeypatch.setattr(plugin, "service", SimpleNamespace(convert=AsyncMock(return_value=result), delete=AsyncMock()))
    monkeypatch.setattr(plugin, "config", SimpleNamespace(allowed_groups=frozenset()))
    monkeypatch.setattr(plugin, "VIDEO_CLEANUP_DELAY_SECONDS", 0.0)
    bot = _Bot()
    bot.send_group_forward_msg = AsyncMock(side_effect=RuntimeError("forward failed"))
    async def run():
        await plugin.handle_video_link(bot, _Event(100))
        shared = plugin._shared_videos[source]
        assert shared.leases == 0
        assert ("100", source) not in plugin._recent
        await shared.cleanup_task
    asyncio.run(run())
    assert bot.messages == []
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

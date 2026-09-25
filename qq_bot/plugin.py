from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass

import httpx
from nonebot import on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageSegment

from app.downloader import video_platform

from .config import BotConfig
from .links import extract_video_urls
from .service import CompletedVideo, JobFailed, VideoServiceClient


logger = logging.getLogger(__name__)
config = BotConfig.from_env()
service = VideoServiceClient(config)
matcher = on_message(priority=20, block=False)
_recent: dict[tuple[str, str], float] = {}
_lock = asyncio.Lock()


def _env_float(name: str, default: float, minimum: float) -> float:
    try:
        return max(float(os.getenv(name, str(default))), minimum)
    except ValueError:
        return default


VIDEO_DEDUPLICATE_SECONDS = 300.0
VIDEO_CLEANUP_DELAY_SECONDS = _env_float(
    "QQ_VIDEO_CLEANUP_DELAY_SECONDS", 180.0, 0.0
)
VIDEO_CLEANUP_RETRY_SECONDS = _env_float(
    "QQ_VIDEO_CLEANUP_RETRY_SECONDS", 30.0, 5.0
)


@dataclass
class _SharedVideo:
    task: asyncio.Task[CompletedVideo]
    leases: int = 0
    video: CompletedVideo | None = None
    cleaning: bool = False
    cleanup_task: asyncio.Task[None] | None = None


_shared_videos: dict[str, _SharedVideo] = {}


async def _is_duplicate(group_id: str, url: str) -> bool:
    now = time.monotonic()
    key = (group_id, url)
    async with _lock:
        expired = [
            item
            for item, timestamp in _recent.items()
            if now - timestamp > VIDEO_DEDUPLICATE_SECONDS
        ]
        for item in expired:
            _recent.pop(item, None)
        if key in _recent:
            return True
        _recent[key] = now
        return False


async def _forget_duplicate(group_id: str, url: str) -> None:
    async with _lock:
        _recent.pop((group_id, url), None)


async def _cleanup_shared_video(url: str, shared: _SharedVideo) -> None:
    """Delete a delivered job after NapCat has had time to fetch the URL."""
    try:
        await asyncio.sleep(VIDEO_CLEANUP_DELAY_SECONDS)
        while True:
            async with _lock:
                if shared.leases or _shared_videos.get(url) is not shared:
                    return
                # A new request that arrives after this point gets a new job
                # instead of racing the deletion of the old file.
                shared.cleaning = True

            try:
                await service.delete(shared.video.job_id)  # type: ignore[union-attr]
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    # The server may already have removed it during restart.
                    pass
                else:
                    logger.exception(
                        "Failed to clean delivered video job %s; retrying",
                        shared.video.job_id,  # type: ignore[union-attr]
                    )
                    async with _lock:
                        shared.cleaning = False
                        if shared.leases or _shared_videos.get(url) is not shared:
                            return
                    await asyncio.sleep(VIDEO_CLEANUP_RETRY_SECONDS)
                    continue
            except Exception:
                logger.exception(
                    "Failed to clean delivered video job %s; retrying",
                    shared.video.job_id,  # type: ignore[union-attr]
                )
                async with _lock:
                    shared.cleaning = False
                    if shared.leases or _shared_videos.get(url) is not shared:
                        return
                await asyncio.sleep(VIDEO_CLEANUP_RETRY_SECONDS)
                continue

            async with _lock:
                if _shared_videos.get(url) is shared:
                    _shared_videos.pop(url, None)
            logger.info("Deleted delivered video job %s", shared.video.job_id)  # type: ignore[union-attr]
            return
    finally:
        async with _lock:
            if shared.cleanup_task is asyncio.current_task():
                shared.cleanup_task = None


def _consume_conversion_exception(task: asyncio.Task[CompletedVideo]) -> None:
    # All callers may have left before a conversion fails.
    if not task.cancelled():
        task.exception()


async def _convert_shared_video(url: str) -> CompletedVideo:
    try:
        video = await service.convert(url)
    except BaseException:
        async with _lock:
            shared = _shared_videos.get(url)
            if shared is not None and shared.task is asyncio.current_task():
                _shared_videos.pop(url, None)
        raise
    async with _lock:
        shared = _shared_videos.get(url)
        if shared is not None and shared.task is asyncio.current_task():
            shared.video = video
            if not shared.leases:
                shared.cleanup_task = asyncio.create_task(_cleanup_shared_video(url, shared))
    return video


async def _acquire_shared_video(url: str) -> tuple[_SharedVideo, CompletedVideo]:
    """Share one conversion task and completed file across groups."""
    async with _lock:
        shared = _shared_videos.get(url)
        if shared is None or shared.cleaning or shared.task.cancelled():
            shared = _SharedVideo(asyncio.create_task(_convert_shared_video(url)))
            shared.task.add_done_callback(_consume_conversion_exception)
            _shared_videos[url] = shared
        elif shared.cleanup_task is not None and not shared.cleanup_task.done():
            shared.cleanup_task.cancel()
            shared.cleanup_task = None
        shared.leases += 1

    try:
        video = await asyncio.shield(shared.task)
    except BaseException:
        await _release_shared_video(url, shared)
        raise
    return shared, video


async def _release_shared_video(url: str, shared: _SharedVideo) -> None:
    async with _lock:
        shared.leases = max(0, shared.leases - 1)
        if (
            shared.leases
            or shared.video is None
            or shared.cleaning
            or (_shared_videos.get(url) is not shared)
        ):
            return
        shared.cleanup_task = asyncio.create_task(_cleanup_shared_video(url, shared))


@matcher.handle()
async def handle_video_link(bot: Bot, event: GroupMessageEvent) -> None:
    group_id = str(event.group_id)
    if config.allowed_groups and group_id not in config.allowed_groups:
        return

    # segment.data 覆盖手机分享产生的 JSON/XML/share 卡片；str(message) 覆盖普通文本。
    payload = [str(event.get_message()), *[segment.data for segment in event.get_message()]]
    urls = extract_video_urls(payload)
    if not urls:
        return
    if await _is_duplicate(group_id, urls[0]):
        if video_platform(urls[0]) == "xiaoheihe":
            return
        await bot.send(
            event,
            MessageSegment.reply(event.message_id) + "这个链接正在处理或刚刚处理过，请稍候。",
        )
        return

    source_url = urls[0]
    shared: _SharedVideo | None = None
    try:
        shared, video = await _acquire_shared_video(source_url)
        await bot.send_group_msg(
            group_id=event.group_id,
            message=MessageSegment.video(video.url, cache=False, proxy=False, timeout=120),
        )
        logger.info(
            "Delivered video from %s to group %s",
            video_platform(source_url),
            group_id,
        )
    except asyncio.CancelledError:
        await _forget_duplicate(group_id, source_url)
        raise
    except JobFailed as exc:
        detail = str(exc)
        await _forget_duplicate(group_id, source_url)
        if detail == "NO_VIDEO":
            logger.info(
                "Ignored non-video %s post: %s",
                video_platform(source_url),
                source_url,
            )
            return
        logger.exception("Video conversion job failed: %s", exc)
        # All known extraction/conversion failures remain in the local log only,
        # including timeouts, oversized files, and X posts without a video.
    except (httpx.HTTPError, OSError):
        await _forget_duplicate(group_id, source_url)
        logger.exception("QQ video delivery failed")
    except Exception:
        await _forget_duplicate(group_id, source_url)
        logger.exception("Unexpected QQ video processing error")
    finally:
        if shared is not None:
            await _release_shared_video(source_url, shared)

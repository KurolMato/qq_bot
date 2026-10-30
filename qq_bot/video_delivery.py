"""Shared QQ delivery for converted videos, used by the link handler and repairs."""

from __future__ import annotations

import logging

from nonebot.adapters.onebot.v11 import Bot, Message, MessageSegment

from app.downloader import video_platform

from .service import CompletedVideo

logger = logging.getLogger(__name__)

_TITLE_PLATFORMS = {"xiaoheihe", "bilibili"}
_IMAGE_NICKNAMES = {"douyin": "抖音图文", "bilibili": "B站图文", "xiaoheihe": "小黑盒图文"}


async def deliver(
    bot: Bot,
    group_id: int | str,
    source_url: str,
    video: CompletedVideo,
    note: str = "",
) -> None:
    """Send one converted post to a group, matching the normal link handler."""
    if note:
        await bot.send_group_msg(group_id=int(group_id), message=note)

    platform = video_platform(source_url)
    image_title = video.title.strip() if platform in _TITLE_PLATFORMS else ""
    if (platform == "douyin" and len(video.image_urls) >= 5) or (
        platform in _TITLE_PLATFORMS and len(video.image_urls) > 5
    ):
        nickname = _IMAGE_NICKNAMES.get(platform, "图文")
        contents = [
            Message(MessageSegment.image(url, cache=False, proxy=False, timeout=120))
            for url in video.image_urls
        ]
        if image_title:
            contents.insert(0, Message(MessageSegment.text(image_title)))
        await bot.send_group_forward_msg(
            group_id=int(group_id),
            messages=[
                MessageSegment.node_custom(
                    user_id=int(bot.self_id),
                    nickname=nickname,
                    content=content,
                )
                for content in contents
            ],
        )
    elif video.image_urls:
        for start in range(0, len(video.image_urls), 10):
            message = Message([
                MessageSegment.image(url, cache=False, proxy=False, timeout=120)
                for url in video.image_urls[start:start + 10]
            ])
            if image_title and start == 0:
                message.insert(0, MessageSegment.text(image_title + "\n"))
            await bot.send_group_msg(group_id=int(group_id), message=message)
    else:
        await bot.send_group_msg(
            group_id=int(group_id),
            message=MessageSegment.video(video.url, cache=False, proxy=False, timeout=120),
        )
    logger.info(
        "Delivered %s from %s to group %s%s",
        f"{len(video.image_urls)} images" if video.image_urls else "video",
        platform,
        group_id,
        " (repair)" if note else "",
    )

"""Prepare optional QQ stickers shared by all DSH reply paths."""

import asyncio
import random
from pathlib import Path

from nonebot.adapters.onebot.v11 import MessageSegment

from .dsh_media import read_sticker, sticker_categories
from .dsh_policy import should_send_sticker


async def sticker_message(root: Path, category: str | None = None) -> MessageSegment | None:
    """Roll once per reply, preferring the model's selection over a fallback."""
    if not should_send_sticker():
        return None
    data = await read_sticker(root, category) if category else None
    if data is None:
        categories = await asyncio.to_thread(sticker_categories, root)
        if not categories:
            return None
        data = await read_sticker(root, random.choice(categories))
    if data is None:
        return None
    # NapCat sends custom QQ stickers as image segments with picSubType=1.
    segment = MessageSegment.image(data)
    segment.data.update(sub_type=1, summary="[动画表情]")
    return segment

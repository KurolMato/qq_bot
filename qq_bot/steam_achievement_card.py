import asyncio
import logging

from nonebot.adapters.onebot.v11 import Message, MessageSegment

from .list_cards import _load_image
from .steam_achievement_ui import render_card

logger = logging.getLogger(__name__)


async def build_message(name, payload, avatar_url=None):
    message = Message(MessageSegment.text(f'{name}在《{payload["title"]}》中获得全成就。'))
    try:
        async with asyncio.timeout(18):
            semaphore = asyncio.Semaphore(3)
            urls = (avatar_url, payload.get('cover'), payload.get('achievement_icon'))
            avatar, cover, icon = await asyncio.gather(*(_load_image(url, semaphore) for url in urls))
            card = await asyncio.to_thread(
                render_card, name, payload['title'], payload['total'], payload.get('minutes'),
                payload.get('achieved_at'), payload['detected_at'], avatar, cover,
                first_achieved_at=payload.get('first_achieved_at'),
                achievement_name=payload.get('achievement_name'),
                achievement_description=payload.get('achievement_description'),
                achievement_percent=payload.get('achievement_percent'),
                achievement_icon=icon,
                last_achievement_count=payload.get('last_achievement_count', 1),
            )
        message += MessageSegment.image(card, cache=False, proxy=False, timeout=30)
    except Exception:
        logger.warning('Steam achievement card unavailable; sending text', exc_info=True)
    return message

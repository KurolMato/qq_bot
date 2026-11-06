import asyncio
import logging
from urllib.parse import urlsplit

from nonebot.adapters.onebot.v11 import Message, MessageSegment

from .list_cards import _load_image
from .steam_achievement_ui import render_card

logger = logging.getLogger(__name__)
ICON_ROUTE_TIMEOUT = 2


async def _load_achievement_icon(url, semaphore):
    if not url:
        return None
    parts = urlsplit(url)
    hosts = ('cdn.akamai.steamstatic.com', 'cdn.cloudflare.steamstatic.com',
             'steamcdn-a.akamaihd.net')
    if (parts.scheme not in ('http', 'https') or parts.netloc.casefold() not in hosts
            or not parts.path.startswith('/steamcommunity/public/images/apps/')):
        return await _load_image(url, semaphore)
    # Old schema URLs can fail or stall. Try modern CDN routes first, keeping
    # each attempt bounded so one dead route cannot consume the card budget.
    for host in hosts:
        candidate = parts._replace(scheme='https', netloc=host).geturl()
        try:
            content = await asyncio.wait_for(_load_image(candidate, semaphore), ICON_ROUTE_TIMEOUT)
        except TimeoutError:
            continue
        if content:
            return content
    return None


async def build_message(name, payload, avatar_url=None):
    message = Message(MessageSegment.text(f'{name}在《{payload["title"]}》中获得全成就。'))
    try:
        async with asyncio.timeout(18):
            semaphore = asyncio.Semaphore(3)
            avatar, cover, icon = await asyncio.gather(
                _load_image(avatar_url, semaphore),
                _load_image(payload.get('cover'), semaphore),
                _load_achievement_icon(payload.get('achievement_icon'), semaphore),
            )
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

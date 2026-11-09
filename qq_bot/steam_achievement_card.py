import asyncio
import logging
import ssl
import httpx
import truststore
from urllib.parse import urlsplit

from nonebot.adapters.onebot.v11 import Message, MessageSegment

from .list_cards import _load_image
from .steam_achievement_ui import render_card

logger = logging.getLogger(__name__)
ICON_ROUTE_TIMEOUT = 3


async def _download_icon(url):
    async with httpx.AsyncClient(trust_env=False, follow_redirects=True, timeout=3,
                                 verify=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)) as client:
        response = await client.get(url)
        response.raise_for_status()
        if len(response.content) > 12 * 1024 * 1024:
            raise ValueError('Achievement icon exceeds size limit')
        return response.content


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
    # Steam's current community UI uses shared CDN community_assets, while
    # GetSchemaForGame can still return the retired steamcommunity path.
    new_path = parts.path.replace('/steamcommunity/public/images/apps/',
                                  '/community_assets/images/apps/', 1)
    candidates = [parts._replace(scheme='https', netloc=host, path=new_path).geturl()
                  for host in ('shared.akamai.steamstatic.com', 'shared.fastly.steamstatic.com')]
    candidates.extend(parts._replace(scheme='https', netloc=host).geturl() for host in hosts)
    for candidate in candidates:
        try:
            content = await asyncio.wait_for(_load_image(candidate, semaphore, download=_download_icon), ICON_ROUTE_TIMEOUT)
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

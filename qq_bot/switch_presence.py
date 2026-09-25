from __future__ import annotations

import asyncio
import json
import logging
from functools import lru_cache
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

import httpx
import nonebot
from nonebot.adapters.onebot.v11 import Bot, Message, MessageSegment
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

from .resilience import supervise


logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "switch_presence.json"


@dataclass(frozen=True)
class PersonConfig:
    name: str
    presence_url: str
    notify_groups: tuple[str, ...]


@dataclass(frozen=True)
class SwitchMonitorConfig:
    enabled: bool
    poll_interval_seconds: float
    notify_groups: tuple[str, ...]
    people: tuple[PersonConfig, ...]


@dataclass(frozen=True)
class SwitchActivity:
    account_name: str
    state: str
    game_name: str | None
    avatar_url: str | None
    game_image_url: str | None
    platform: str = "Switch"

    @property
    def game_key(self) -> str | None:
        if not self.game_name or self.state.upper() not in {"ONLINE", "PLAYING"}:
            return None
        return self.game_name.casefold()


def _strings(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def _is_safe_presence_url(value: str) -> bool:
    parsed = urlparse(value)
    if parsed.username or parsed.password:
        return False
    if parsed.scheme == "https" and parsed.hostname:
        return True
    return parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}


def load_switch_config(path: Path = CONFIG_PATH) -> SwitchMonitorConfig:
    if not path.is_file():
        return SwitchMonitorConfig(False, 60.0, (), ())
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("switch_presence.json 顶层必须是 JSON 对象")

    default_groups = _strings(raw.get("notify_groups"))
    people: list[PersonConfig] = []
    for index, item in enumerate(raw.get("people", []), start=1):
        if not isinstance(item, dict):
            raise ValueError(f"people[{index}] 必须是 JSON 对象")
        name = str(item.get("name", "")).strip()
        url = str(item.get("presence_url", "")).strip()
        if not name or not _is_safe_presence_url(url):
            raise ValueError(f"people[{index}] 必须填写 name 和 HTTPS presence_url（本机地址可用 HTTP）")
        groups = _strings(item.get("notify_groups")) or default_groups
        if not groups:
            raise ValueError(f"people[{index}] 没有配置通知群号")
        people.append(PersonConfig(name, url, groups))

    interval = max(float(raw.get("poll_interval_seconds", 60)), 30.0)
    return SwitchMonitorConfig(bool(raw.get("enabled", False)), interval, default_groups, tuple(people))


def _mapping_candidates(value: Any) -> list[Mapping[str, Any]]:
    result: list[Mapping[str, Any]] = []
    if isinstance(value, Mapping):
        result.append(value)
        for child in value.values():
            result.extend(_mapping_candidates(child))
    elif isinstance(value, list):
        for child in value:
            result.extend(_mapping_candidates(child))
    return result


def _text(mapping: Mapping[str, Any] | None, *keys: str) -> str | None:
    if not mapping:
        return None
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def parse_switch_activity(payload: Any) -> SwitchActivity:
    """Parse nxapi friend endpoints and nxapi-auth Presence URL responses."""
    candidates = _mapping_candidates(payload)
    profile = next(
        (item for item in candidates if isinstance(item.get("presence"), Mapping)),
        None,
    )
    if profile is None:
        # Some Presence URLs return the presence object as the top-level response.
        profile = next(
            (item for item in candidates if "state" in item and isinstance(item.get("game"), (Mapping, type(None)))),
            None,
        )
        presence = profile
    else:
        presence = profile.get("presence")
    if not isinstance(profile, Mapping) or not isinstance(presence, Mapping):
        raise ValueError("响应中没有可识别的 Switch presence 数据")

    game = presence.get("game")
    game_mapping = game if isinstance(game, Mapping) else None
    account_name = _text(profile, "name", "nickname", "displayName") or "Switch 玩家"
    avatar_url = _text(profile, "imageUri", "imageUrl", "avatarUrl", "avatar_url")

    # Public Presence API variants may keep profile data beside the presence node.
    if not avatar_url:
        avatar_owner = next(
            (item for item in candidates if _text(item, "imageUri", "avatarUrl") and _text(item, "name", "nickname")),
            None,
        )
        avatar_url = _text(avatar_owner, "imageUri", "imageUrl", "avatarUrl", "avatar_url")
        account_name = _text(avatar_owner, "name", "nickname", "displayName") or account_name

    return SwitchActivity(
        account_name=account_name,
        state=(_text(presence, "state", "status") or "OFFLINE").upper(),
        game_name=_text(game_mapping, "name", "title", "titleName"),
        avatar_url=avatar_url,
        game_image_url=_text(game_mapping, "imageUri", "imageUrl", "image_url", "coverUrl"),
        platform="Switch",
    )


def _resize_image(content: bytes, max_width: int, max_height: int) -> bytes:
    with Image.open(BytesIO(content)) as source:
        image = ImageOps.exif_transpose(source)
        image.thumbnail((max_width, max_height), Image.Resampling.LANCZOS)
        output = BytesIO()
        if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
            image.convert("RGBA").save(output, format="PNG", optimize=True)
        else:
            image.convert("RGB").save(output, format="JPEG", quality=82, optimize=True)
        return output.getvalue()


async def _download_image(url: str | None) -> bytes | None:
    if not url:
        return None
    last_error: Exception | None = None
    response: httpx.Response | None = None
    # Nintendo CDN downloads should not depend on a stale system proxy. Try a
    # direct connection first, then retain the environment proxy as a fallback.
    for trust_env in (False, True):
        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=20,
                trust_env=trust_env,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 Chrome/128.0 Safari/537.36"
                    ),
                    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                },
            ) as client:
                response = await client.get(url)
                response.raise_for_status()
            break
        except httpx.HTTPError as exc:
            last_error = exc
            response = None
    if response is None:
        raise last_error or httpx.ConnectError("图片下载失败")
    if len(response.content) > 12 * 1024 * 1024:
        raise ValueError("图片超过 12 MB")
    content_type = response.headers.get("content-type", "").lower()
    if content_type and not content_type.startswith("image/"):
        # Nintendo eShop CDN serves valid JPEG files as
        # application/octet-stream. Verify the bytes instead of trusting the
        # generic MIME type, while still rejecting HTML/error responses.
        try:
            with Image.open(BytesIO(response.content)) as image:
                image.verify()
        except Exception as exc:
            raise ValueError(f"响应不是图片：{content_type}") from exc
    return response.content


async def _download_card_image(url: str | None, kind: str) -> bytes | None:
    try:
        return await _download_image(url)
    except Exception as exc:
        logger.warning(
            "Failed to download Switch %s for activity card: %s (%s)",
            kind,
            url,
            type(exc).__name__,
        )
        return None


async def small_image_segment(
    url: str | None, max_width: int, max_height: int
) -> MessageSegment | None:
    if not url:
        return None
    try:
        content = await _download_image(url)
        if content is None:
            return None
        resized = await asyncio.to_thread(_resize_image, content, max_width, max_height)
        return MessageSegment.image(resized, cache=False, proxy=False, timeout=30)
    except Exception as exc:
        logger.warning(
            "Failed to download or resize Switch image: %s (%s)", url, type(exc).__name__
        )
        return None


@lru_cache(maxsize=64)
def _font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        Path("C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/segoeuib.ttf" if bold else "C:/Windows/Fonts/segoeui.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else
             "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    for path in candidates:
        if path.is_file():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default()


def _fitted_text(
    draw: ImageDraw.ImageDraw, text: str, max_width: int, start_size: int, min_size: int
) -> tuple[str, ImageFont.FreeTypeFont | ImageFont.ImageFont]:
    value = text.strip() or "Switch 玩家"
    size = start_size
    font = _font(size, bold=True)
    while size > min_size and draw.textbbox((0, 0), value, font=font)[2] > max_width:
        size -= 1
        font = _font(size, bold=True)
    if draw.textbbox((0, 0), value, font=font)[2] <= max_width:
        return value, font
    while value and draw.textbbox((0, 0), value + "…", font=font)[2] > max_width:
        value = value[:-1]
    return value + "…", font


def _card_image(content: bytes | None, size: tuple[int, int], colour: str) -> Image.Image:
    if content:
        with Image.open(BytesIO(content)) as source:
            return ImageOps.fit(
                ImageOps.exif_transpose(source).convert("RGB"),
                size,
                method=Image.Resampling.LANCZOS,
                centering=(0.5, 0.5),
            )
    return Image.new("RGB", size, colour)


def _card_backdrop(
    content: bytes | None,
    size: tuple[int, int],
    *,
    shade: int = 92,
) -> Image.Image | None:
    """Build a recognisable, lightly softened game-art background."""
    if not content:
        return None
    with Image.open(BytesIO(content)) as source:
        image = ImageOps.fit(
            ImageOps.exif_transpose(source).convert("RGB"),
            size,
            method=Image.Resampling.LANCZOS,
            centering=(0.48, 0.45),
        )
    image = ImageEnhance.Brightness(image).enhance(0.92)
    image = ImageEnhance.Color(image).enhance(0.82)
    image = image.filter(ImageFilter.GaussianBlur(max(1, size[1] // 90)))
    base = image.convert("RGBA")
    base.alpha_composite(Image.new("RGBA", size, (5, 14, 24, shade)))
    # Increase contrast gradually towards the text side of the card.
    gradient = Image.new("L", (size[0], 1))
    gradient.putdata([
        int(22 + 88 * x / max(size[0] - 1, 1))
        for x in range(size[0])
    ])
    gradient = gradient.resize(size)
    return Image.composite(Image.new("RGB", size, "#020911"), base.convert("RGB"), gradient)


def _rounded_paste(
    canvas: Image.Image, image: Image.Image, position: tuple[int, int], radius: int
) -> None:
    mask = Image.new("L", image.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, image.width - 1, image.height - 1), radius=radius, fill=255
    )
    canvas.paste(image, position, mask)


def _render_activity_card(
    display_name: str,
    game_name: str,
    avatar_content: bytes | None,
    cover_content: bytes | None,
    platform: str = "Switch",
) -> bytes:
    canvas = Image.new("RGB", (640, 220), "#0b131b")
    draw = ImageDraw.Draw(canvas)
    background = _card_backdrop(cover_content, (612, 192))
    if background is not None:
        _rounded_paste(canvas, background, (14, 14), 24)
        draw.rounded_rectangle((14, 14, 626, 206), radius=24, outline="#58728a")
    else:
        draw.rounded_rectangle((14, 14, 626, 206), radius=24, fill="#141e2a", outline="#263746")

    cover = _card_image(cover_content, (130, 176), "#263647")
    avatar = _card_image(avatar_content, (64, 64), "#35485c")
    _rounded_paste(canvas, cover, (30, 28), 15)
    _rounded_paste(canvas, avatar, (180, 34), 12)

    # The dot + label occupy x=274..354. Extending the right edge to 366
    # centres that complete visual group inside the green pill.
    draw.rounded_rectangle((262, 34, 366, 64), radius=15, fill="#247e55")
    draw.ellipse((274, 46, 282, 54), fill="#58dda0")
    draw.text((290, 39), "正在游玩", font=_font(16), fill="#74e3af")

    normalized_platform = platform.strip().casefold()
    is_playstation = normalized_platform.startswith(("ps", "playstation"))
    is_steam = normalized_platform.startswith("steam")
    is_xbox = normalized_platform.startswith("xbox")
    platform_label = "Xbox" if is_xbox else "Steam" if is_steam else "PS" if is_playstation else "Switch"
    platform_colour = "#107c10" if is_xbox else "#0b5f91" if is_steam else "#0070d1" if is_playstation else "#e60012"
    platform_width = 68 if is_xbox else 72 if is_steam else 54 if is_playstation else 82
    platform_left = 376
    platform_right = platform_left + platform_width
    draw.rounded_rectangle(
        (platform_left, 34, platform_right, 64),
        radius=15,
        fill=platform_colour,
    )
    platform_font = _font(16, bold=True)
    platform_box = draw.textbbox((0, 0), platform_label, font=platform_font)
    platform_text_width = platform_box[2] - platform_box[0]
    draw.text(
        (platform_left + (platform_width - platform_text_width) // 2, 39),
        platform_label,
        font=platform_font,
        fill="#ffffff",
    )

    nickname, nickname_font = _fitted_text(draw, display_name, 344, 27, 19)
    draw.text((262, 75), nickname, font=nickname_font, fill="#f4f7fb")

    title, title_font = _fitted_text(draw, game_name, 420, 30, 19)
    draw.text((180, 128), title, font=title_font, fill="#ffffff")

    output = BytesIO()
    canvas.save(output, format="PNG", optimize=True)
    return output.getvalue()


async def build_activity_message(display_name: str, activity: SwitchActivity) -> Message:
    try:
        avatar_content, cover_content = await asyncio.gather(
            _download_card_image(activity.avatar_url, "avatar"),
            _download_card_image(activity.game_image_url, "cover"),
        )
        card = await asyncio.to_thread(
            _render_activity_card,
            display_name,
            activity.game_name or "未知游戏",
            avatar_content,
            cover_content,
            activity.platform,
        )
        return Message(MessageSegment.image(card, cache=False, proxy=False, timeout=30))
    except Exception:
        logger.warning("Failed to build Switch activity card", exc_info=True)
        return Message(MessageSegment.text(f"{display_name} 开始玩《{activity.game_name}》"))


async def _nxapi_has_subscriptions() -> bool:
    """Return whether the preferred Nxapi Switch monitor has any records.

    The legacy ``switch_presence.json`` monitor is retained for installations
    that have not migrated yet, but it must yield as soon as the Nxapi-backed
    registry is in use. Import lazily to avoid a module import cycle because
    ``switch_registry`` reuses this module's card helpers.
    """

    try:
        from .switch_registry import registry as nxapi_registry

        return bool(await asyncio.to_thread(nxapi_registry.list_all))
    except Exception as exc:
        # A registry inspection failure should not permanently disable the
        # legacy configuration. The Nxapi monitor will report its own error.
        logger.warning("Unable to inspect Nxapi Switch subscriptions: %s", type(exc).__name__)
        return False


class SwitchPresenceMonitor:
    def __init__(self, config: SwitchMonitorConfig) -> None:
        self.config = config
        self._previous_games: dict[str, str | None] = {}
        self._initialised: set[str] = set()
        self._stop = asyncio.Event()

    async def stop(self) -> None:
        self._stop.set()

    async def fetch_activity(self, client: httpx.AsyncClient, person: PersonConfig) -> SwitchActivity:
        response = await client.get(person.presence_url)
        response.raise_for_status()
        return parse_switch_activity(response.json())

    async def poll_person(self, client: httpx.AsyncClient, person: PersonConfig) -> None:
        activity = await self.fetch_activity(client, person)
        current_game = activity.game_key

        if person.name not in self._initialised:
            self._previous_games[person.name] = current_game
            self._initialised.add(person.name)
            logger.info("Switch presence baseline ready for %s", person.name)
            return

        previous_game = self._previous_games.get(person.name)
        if current_game == previous_game:
            return

        # Offline/idle transitions only reset state; notifications are for starts/switches.
        if current_game is None:
            self._previous_games[person.name] = None
            return

        bots = [bot for bot in nonebot.get_bots().values() if isinstance(bot, Bot)]
        if not bots:
            logger.warning("Switch activity changed but no OneBot connection is available")
            return

        # Nxapi is the preferred monitor. Re-check immediately before
        # delivery so a subscription added after startup cannot make the two
        # monitors announce the same transition.
        if await _nxapi_has_subscriptions():
            return

        message = await build_activity_message(person.name, activity)
        successful = False
        for group_id in person.notify_groups:
            try:
                await bots[0].send_group_msg(group_id=int(group_id), message=message)
                successful = True
            except Exception:
                logger.exception("Failed to send Switch presence to group %s", group_id)
        if successful:
            self._previous_games[person.name] = current_game

    async def run(self) -> None:
        timeout = httpx.Timeout(20.0, connect=10.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            suspended_for_nxapi = False
            while not self._stop.is_set():
                if await _nxapi_has_subscriptions():
                    if not suspended_for_nxapi:
                        logger.info(
                            "Legacy Switch presence monitoring paused because Nxapi subscriptions exist"
                        )
                    suspended_for_nxapi = True
                    try:
                        await asyncio.wait_for(self._stop.wait(), timeout=self.config.poll_interval_seconds)
                    except TimeoutError:
                        pass
                    continue
                if suspended_for_nxapi:
                    logger.info("Legacy Switch presence monitoring resumed (Nxapi registry is empty)")
                    suspended_for_nxapi = False
                for person in self.config.people:
                    try:
                        await self.poll_person(client, person)
                    except (httpx.HTTPError, ValueError, json.JSONDecodeError):
                        logger.exception("Failed to poll Switch presence for %s", person.name)
                    except Exception:
                        logger.exception("Unexpected direct Switch presence failure for %s", person.name)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.config.poll_interval_seconds)
                except TimeoutError:
                    pass


_monitor: SwitchPresenceMonitor | None = None
_task: asyncio.Task[None] | None = None


async def start_switch_monitor() -> None:
    global _monitor, _task
    try:
        config = load_switch_config()
    except (OSError, ValueError, json.JSONDecodeError):
        logger.exception("Invalid switch_presence.json; Switch monitoring is disabled")
        return
    if not config.enabled or not config.people:
        logger.info("Switch presence monitoring is disabled")
        return
    if await _nxapi_has_subscriptions():
        logger.info(
            "Legacy Switch presence monitoring is disabled because Nxapi subscriptions exist"
        )
        return
    _monitor = SwitchPresenceMonitor(config)
    _task = asyncio.create_task(
        supervise("switch-presence-monitor", _monitor.run, _monitor._stop.is_set),
        name="switch-presence-monitor-supervisor",
    )
    logger.info("Switch presence monitoring started for %d people", len(config.people))


async def stop_switch_monitor() -> None:
    global _monitor, _task
    if _monitor:
        await _monitor.stop()
    if _task:
        await _task
    _monitor = None
    _task = None

"""Build DSH input from OneBot images, replies and forwarded messages.

No matchers, background tasks or persistent state are created on import.
Limits are passed by callers so group and private chat use the same rules.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
from pathlib import Path

import httpx
from nonebot.adapters.onebot.v11 import Bot, Message, MessageSegment, PrivateMessageEvent

from .dsh_client import DshError, ImagePart
from .dsh_media import MAX_IMAGE_BYTES, sniff_image_mime
from .qq_forward import flatten_forward

logger = logging.getLogger(__name__)

# A full browser UA: several hosts (Wikimedia upload, some CDNs) reject the
# bare "Mozilla/5.0" token that older builds sent, which broke 收图 from them.
IMAGE_FETCH_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def local_path(raw: str) -> Path | None:
    if raw.startswith("file://"):
        from urllib.parse import unquote, urlparse

        path = unquote(urlparse(raw).path)
        if len(path) > 2 and path[0] == "/" and path[2] == ":":
            path = path[1:]
        return Path(path)
    candidate = Path(raw)
    return candidate if candidate.exists() else None


async def fetch_image(raw_url: str) -> tuple[bytes, str] | None:
    """Download (or read) one image and keep it only when it is a real, small image."""
    try:
        if raw_url.startswith("base64://"):
            data = base64.b64decode(raw_url[len("base64://"):], validate=False)
        elif raw_url.startswith(("http://", "https://")):
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as http:
                for attempt in range(3):
                    try:
                        response = await http.get(raw_url, headers={"User-Agent": IMAGE_FETCH_USER_AGENT})
                        response.raise_for_status()
                        data = response.content
                        break
                    except httpx.TransportError as exc:
                        logger.info("图片下载第 %s 次失败：%s: %s", attempt + 1, type(exc).__name__, exc)
                        if attempt == 2:
                            raise
                        await asyncio.sleep(0.5 * (attempt + 1))
        else:
            path = local_path(raw_url)
            if path is None:
                return None
            data = await asyncio.to_thread(path.read_bytes)
    except (OSError, ValueError, binascii.Error, httpx.HTTPError) as exc:
        logger.info("图片读取失败：%s: %s", type(exc).__name__, exc)
        return None
    if not data or len(data) > MAX_IMAGE_BYTES:
        return None
    mime = sniff_image_mime(data)
    if mime is None:
        return None
    return data, mime


def image_refs(message: Message) -> list[dict[str, str]]:
    """Every image segment of a message, with the raw fields the client gave us."""
    refs: list[dict[str, str]] = []
    for segment in message:
        if segment.type not in {"image", "mface"}:
            continue
        data = segment.data or {}
        ref = {
            "url": str(data.get("url") or ""),
            "file": str(data.get("file") or ""),
            "base64": str(data.get("base64") or ""),
        }
        if any(ref.values()):
            refs.append(ref)
    return refs


def image_urls(message: Message) -> list[str]:
    return [ref["url"] or ref["file"] for ref in image_refs(message)]


async def _image_from_client(bot: Bot, ref: dict[str, str]) -> tuple[bytes, str] | None:
    """Ask NapCat itself for the bytes; the signed qpic link often answers 400."""
    call_api = getattr(bot, "call_api", None)
    if call_api is None:
        return None
    for key in ("file", "url"):
        raw = ref.get(key)
        if not raw:
            continue
        try:
            payload = await call_api("get_image", file=raw)
        except Exception as exc:  # noqa: BLE001 - fallback only
            logger.info("get_image 调用失败（%s）：%s: %s", key, type(exc).__name__, exc)
            continue
        if not isinstance(payload, dict):
            continue
        for field in ("base64", "file"):
            candidate = payload.get(field)
            if not isinstance(candidate, str) or not candidate:
                continue
            if field == "base64" and not candidate.startswith("base64://"):
                candidate = "base64://" + candidate
            fetched = await fetch_image(candidate)
            if fetched is not None:
                return fetched
    return None


async def fetch_ref(bot: Bot | None, ref: dict[str, str]) -> tuple[bytes, str] | None:
    for raw in (ref.get("base64"), ref.get("url"), ref.get("file")):
        if not raw:
            continue
        if raw.startswith("base64://") or raw.startswith(("http://", "https://")) or Path(raw).exists():
            fetched = await fetch_image(raw)
            if fetched is not None:
                return fetched
    if bot is not None:
        fetched = await _image_from_client(bot, ref)
        if fetched is not None:
            return fetched
    logger.info("图片取不到：url=%s file=%s", ref.get("url", "")[:80], ref.get("file", "")[:80])
    return None


async def fetch_images(bot: Bot | None, refs: list[dict[str, str]], limit: int) -> list[ImagePart]:
    images: list[ImagePart] = []
    for ref in refs:
        if len(images) >= limit:
            break
        fetched = await fetch_ref(bot, ref)
        if fetched is None:
            continue
        images.append(ImagePart(fetched[0], fetched[1]))
    return images


async def collect_images(bot: Bot, event: PrivateMessageEvent, max_images: int) -> list[ImagePart]:
    if max_images <= 0:
        return []
    return await fetch_images(bot, image_refs(event.get_message()), max_images)


async def forward_text(bot: Bot, event: PrivateMessageEvent) -> str:
    """Read merged/forwarded chat records so the agent can see them."""
    chunks: list[str] = []
    for segment in event.get_message():
        if segment.type != "forward":
            continue
        forward_id = str(segment.data.get("id") or "")
        if not forward_id:
            continue
        try:
            payload = await bot.get_forward_msg(id=forward_id)
        except Exception:  # noqa: BLE001 - a stale forward must not break the turn
            logger.info("转发聊天记录读取失败：%s", forward_id)
            continue
        text = flatten_forward(payload if isinstance(payload, dict) else {})
        if text:
            chunks.append(text)
    return "\n\n".join(chunks)


async def quoted_message(bot: Bot, event) -> Message:
    """Use the adapter's resolved reply, falling back to OneBot when necessary."""
    reply = getattr(event, "reply", None)
    raw = getattr(event, "_dsh_quoted_message", None)
    if raw is None:
        raw = getattr(reply, "message", None)
    message_id = getattr(reply, "message_id", None)
    if message_id is None:
        for segment in event.get_message():
            if segment.type == "reply":
                message_id = segment.data.get("id")
                break
    if raw is None:
        if message_id is None:
            return Message()
        try:
            payload = await bot.get_msg(message_id=message_id)
        except Exception as exc:
            logger.info("引用消息读取失败：%s: %s", type(exc).__name__, exc)
            raise DshError("那条引用消息没读到，重新发一下吧。") from exc
        raw = payload.get("message") if isinstance(payload, dict) else None
    if isinstance(raw, (str, Message)):
        return Message(raw)
    if not isinstance(raw, list):
        raise DshError("那条引用消息没读到，重新发一下吧。")
    message = Message()
    for segment in raw:
        if isinstance(segment, MessageSegment):
            message.append(segment)
        elif isinstance(segment, dict) and isinstance(segment.get("data"), dict):
            message.append(MessageSegment(str(segment.get("type") or ""), segment["data"]))
    return message


async def quoted_images(bot: Bot, event: PrivateMessageEvent, *, max_images: int, strict: bool = False) -> list[ImagePart]:
    try:
        message = await quoted_message(bot, event)
    except DshError:
        if strict:
            raise
        return []
    refs = image_refs(message)
    images = await fetch_images(bot, refs, max(max_images, 1))
    if strict and len(images) < min(len(refs), max(max_images, 1)):
        raise DshError("引用图片下载不完整")
    return images


async def question_context(bot: Bot, event, text: str, *, max_images: int, max_question_chars: int) -> tuple[str, list[ImagePart]]:
    """Attach quoted text and both sets of images in an explicit, stable order."""
    quoted = await quoted_message(bot, event)
    current_refs = image_refs(event.get_message())
    quoted_refs = image_refs(quoted)
    refs = current_refs + quoted_refs
    limit = min(max_images, 4)  # ACP client accepts at most four images per turn.
    if len(refs) > limit:
        raise DshError(f"这条消息和引用里一共有 {len(refs)} 张图，一次最多能看 {limit} 张，分开发吧。")
    images = await fetch_images(bot, refs, limit)
    if len(images) != len(refs):
        raise DshError("有图片没下载下来，重新发图吧。")
    quoted_text = quoted.extract_plain_text().strip()
    if quoted_text:
        if len(quoted_text) > max_question_chars:
            quoted_text = quoted_text[:max_question_chars] + "\n（引用内容过长，后文已截断）"
        text += "\n\n【被引用的原文，仅作上下文】\n" + quoted_text + "\n【引用结束】"
    if refs:
        text += f"\n（附件顺序：当前消息图片 {len(current_refs)} 张，引用消息图片 {len(quoted_refs)} 张。）"
    return text, images

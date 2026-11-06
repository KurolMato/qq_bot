"""Automatic repair for group video links that failed to deliver.

A failed link is retried a few times with backoff; when a retry succeeds the
video is sent to the same group. Only after every retry fails does it ask the
read-only DSH agent for a diagnosis and report that to the owner privately.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime

from nonebot.adapters.onebot.v11 import Bot

from .service import CompletedVideo, JobFailed, VideoServiceClient
from .video_delivery import DeliveryUncertain, deliver

logger = logging.getLogger(__name__)

_pending: set[str] = set()
_tasks: set[asyncio.Task] = set()
_delivered: dict[str, float] = {}


def _env_flag(name: str, default: str = "true") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def parse_delays(raw: str, default: str = "45,180,600") -> list[float]:
    """Parse a comma separated list of retry delays in seconds."""
    text = (raw or "").strip() or default
    delays: list[float] = []
    for item in text.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            value = float(item)
        except ValueError:
            continue
        if value >= 0:
            delays.append(value)
    return delays or parse_delays(default, "")


def repair_key(group_id: str, source_url: str) -> str:
    return f"{group_id}|{source_url}"


def is_repair_pending(group_id: str, source_url: str) -> bool:
    now = time.monotonic()
    for key, until in list(_delivered.items()):
        if until <= now:
            _delivered.pop(key, None)
    key = repair_key(group_id, source_url)
    return key in _pending or key in _delivered


def schedule_repair(
    bot: Bot,
    group_id: str,
    source_url: str,
    reason: str,
    service: VideoServiceClient,
) -> None:
    """Queue one repair; repeated failures for the same link are ignored."""
    if not _env_flag("QQ_REPAIR_ENABLED", "true"):
        return
    if any(marker in reason for marker in ("NO_VIDEO", "超过群发送上限")):
        # Deterministic outcomes: retrying cannot change them.
        return
    key = repair_key(group_id, source_url)
    if is_repair_pending(group_id, source_url):
        return
    _pending.add(key)
    task = asyncio.create_task(_repair(bot, group_id, source_url, reason, service, key))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _repair(
    bot: Bot,
    group_id: str,
    source_url: str,
    reason: str,
    service: VideoServiceClient,
    key: str,
) -> None:
    delays = parse_delays(os.getenv("QQ_REPAIR_DELAYS", "45,180,600"))
    note = os.getenv("QQ_REPAIR_NOTE", "").strip()
    last_error = reason
    try:
        for attempt, delay in enumerate(delays, start=1):
            await asyncio.sleep(delay)
            try:
                video = await service.convert(source_url)
                await deliver(bot, group_id, source_url, video, note=note)
                _delivered[key] = time.monotonic() + 300
                logger.info("链接补发成功（第 %s 次）：%s -> 群 %s", attempt, source_url, group_id)
                _schedule_cleanup(service, video)
                return
            except DeliveryUncertain:
                _delivered[key] = time.monotonic() + 300
                logger.warning("补发发送结果不确定，停止自动重发：%s -> 群 %s", source_url, group_id)
                _schedule_cleanup(service, video)
                return
            except (JobFailed, asyncio.TimeoutError, OSError) as exc:
                last_error = str(exc)
                logger.warning("补发第 %s 次失败：%s（%s）", attempt, source_url, exc)
            except Exception as exc:  # noqa: BLE001 - one bad retry must not kill the task
                last_error = str(exc)
                logger.exception("补发出现未预期错误：%s", source_url)
        logger.warning("链接补发最终失败：%s（%s）", source_url, last_error)
        if _env_flag("QQ_REPAIR_DIAG", "true"):
            await _diagnose_and_report(bot, group_id, source_url, last_error)
    finally:
        _pending.discard(key)


def _schedule_cleanup(service: VideoServiceClient, video: CompletedVideo) -> None:
    async def _cleanup() -> None:
        await asyncio.sleep(300)
        try:
            await service.delete(video.job_id)
        except Exception:  # noqa: BLE001 - cleanup is best effort
            logger.debug("补发视频清理失败：%s", video.job_id)

    task = asyncio.create_task(_cleanup())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _diagnose_and_report(bot: Bot, group_id: str, source_url: str, reason: str) -> None:
    owners = [item.strip() for item in os.getenv("QQ_DSH_USERS", "").split(",") if item.strip()]
    if not owners:
        return
    try:
        from . import dsh_commands
    except Exception:  # noqa: BLE001 - diagnosis is optional
        logger.debug("DSH 插件未加载，跳过自动排查")
        return
    prompt = (
        f"群里一条链接没发出来，已经重试过几次都失败了。\n"
        f"链接：{source_url}\n群号：{group_id}\n最后一次错误：{reason}\n"
        f"时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        "只读排查：看 data/qq-bot.log 最近 200 行和 data/video-api.log 里的相关报错，"
        "一句话说原因，一句话说建议。不要改文件。"
    )
    session_id = ""
    try:
        session_id = await asyncio.to_thread(dsh_commands.readonly_client.new_session)
        answer = await asyncio.to_thread(dsh_commands.readonly_client.prompt, session_id, prompt)
        text = (answer.text or "").strip()[:600]
    except Exception:  # noqa: BLE001
        logger.exception("自动排查失败")
        return
    finally:
        if session_id:
            try:
                await asyncio.to_thread(dsh_commands.readonly_client.close_session, session_id)
            except Exception:  # noqa: BLE001
                pass
    if not text:
        return
    message = f"有条链接补发失败：{source_url[:120]}\n{text}"
    for owner in owners:
        try:
            await bot.send_private_msg(user_id=int(owner), message=message)
        except Exception:  # noqa: BLE001
            logger.debug("排查结果私聊发送失败：%s", owner)

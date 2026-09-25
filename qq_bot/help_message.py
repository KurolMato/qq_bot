from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from nonebot import on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageSegment
from nonebot.rule import Rule, to_me

from .config import BotConfig


config = BotConfig.from_env()
logger = logging.getLogger(__name__)
HELP_IMAGE = Path(__file__).resolve().parent.parent / "assets" / "instruction.png"
HELP_TEXT = (
    "常用指令：/bot status、/game list、/rank、/hltb 游戏名、"
    "/switch list、/psn list、/steam list、/xbox list。"
)


async def _is_direct_mention_without_command(bot: Bot, event: GroupMessageEvent) -> bool:
    # OneBot removes the leading/trailing @ segment from event.message before
    # matcher rules run; original_message keeps reply/at/text and other raw segments.
    mention_count = 0
    for segment in event.original_message:
        if segment.type == "text" and not str(segment.data.get("text", "")).strip():
            continue
        if segment.type == "at" and str(segment.data.get("qq")) == str(bot.self_id):
            mention_count += 1
            continue
        # reply、图片、链接、命令文字、@其他人等任何额外内容都不是“单独@机器人”。
        return False
    return mention_count == 1


help_matcher = on_message(
    rule=to_me() & Rule(_is_direct_mention_without_command),
    priority=5,
    block=True,
)


@help_matcher.handle()
async def send_help(bot: Bot, event: GroupMessageEvent) -> None:
    if config.allowed_groups and str(event.group_id) not in config.allowed_groups:
        return
    if not HELP_IMAGE.is_file():
        await bot.send(event, HELP_TEXT)
        return
    try:
        content = await asyncio.to_thread(HELP_IMAGE.read_bytes)
        await bot.send(
            event,
            MessageSegment.image(content, cache=False, proxy=False, timeout=30),
        )
    except Exception:
        logger.exception("Failed to send instruction image")
        await bot.send(event, "使用说明图片暂时无法发送，请稍后再试。")

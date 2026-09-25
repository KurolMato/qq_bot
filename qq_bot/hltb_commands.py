from __future__ import annotations

import logging
from pathlib import Path

import nonebot
from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageSegment
from nonebot.params import CommandArg

from .config import BotConfig
from .hltb_service import HltbError, HltbService, UNAVAILABLE, format_result

logger = logging.getLogger(__name__)
config = BotConfig.from_env()
service = HltbService(Path(__file__).resolve().parent.parent / "data" / "hltb-cache.db")
hltb_command = on_command("hltb", priority=10, block=True)
nonebot.get_driver().on_shutdown(service.close)


@hltb_command.handle()
async def handle_hltb(bot: Bot, event: GroupMessageEvent, argument: Message = CommandArg()):
    if config.allowed_groups and str(event.group_id) not in config.allowed_groups:
        return
    value = argument.extract_plain_text().strip()
    owner = (str(event.group_id), str(event.user_id))
    parts = value.split()
    try:
        if parts and parts[0].casefold() == "select":
            if len(parts) != 2 or not parts[1].isascii() or not parts[1].isdigit():
                raise HltbError("用法：/hltb select 序号")
            result = await service.select(int(parts[1]), owner)
        else:
            result = await service.query(value, owner)
        text = format_result(result)
    except HltbError as exc:
        text = str(exc)
    except Exception:
        logger.exception("HLTB command failed")
        text = UNAVAILABLE
    await bot.send(event, MessageSegment.reply(event.message_id) + MessageSegment.text(text))

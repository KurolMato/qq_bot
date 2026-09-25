from __future__ import annotations

import asyncio
import logging
import sqlite3
import time

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageSegment
from nonebot.params import CommandArg

from .config import BotConfig
from .list_cards import ListCardEntry, build_list_message
from .nicknames import list_line, normalize_nickname
from .ps5_registry import (
    Ps5Error,
    Ps5Subscription,
    Ps5VisibilityError,
    normalize_online_id,
    ps5_client,
    registry,
)
from .switch_presence import small_image_segment


config = BotConfig.from_env()
logger = logging.getLogger(__name__)
ps5_command = on_command("psn", priority=10, block=True)
HELP = (
    "PS5 视奸命令：\n"
    "/psn add PSN在线ID [昵称]  检查公开状态并登记\n"
    "/psn list  查看本群视奸列表\n"
    "/psn nickname PSN在线ID 昵称  修改昵称\n"
    "/psn remove PSN在线ID  取消本群视奸\n"
    "/psn status  检查 PSN 观察账号状态"
)


@ps5_command.handle()
async def handle_ps5_command(bot: Bot, event: GroupMessageEvent, argument: Message = CommandArg()) -> None:
    group_id = str(event.group_id)
    user_id = str(event.user_id)
    if config.allowed_groups and group_id not in config.allowed_groups:
        return

    parts = argument.extract_plain_text().strip().split(maxsplit=2)
    action = parts[0].lower() if parts else "help"

    if action in {"help", "帮助", "?"}:
        await bot.send(event, HELP)
        return

    if action == "status":
        if not ps5_client.installed:
            await bot.send(event, "PS5 功能尚未安装，请联系机器人管理员。")
            return
        if not ps5_client.configured:
            await bot.send(event, "PSN 观察账号尚未登录，请在机器人电脑的管理器中选择“登录 PSN 观察账号”。")
            return
        try:
            await ps5_client.status()
            await bot.send(event, "PS5 服务正常。")
        except Ps5Error as exc:
            await bot.send(event, str(exc))
        except Exception:
            logger.exception("Unexpected PS5 status error")
            await bot.send(event, "PSN 状态暂时无法查询，请稍后再试。")
        return

    if action == "list":
        # This is a tiny indexed local query. Running it directly avoids waiting
        # behind PSN network calls in asyncio's shared worker queue.
        started = time.perf_counter()
        items = registry.list_group(group_id)
        database_ms = (time.perf_counter() - started) * 1000
        if not items:
            await bot.send(event, "本群还没有登记 PS5 视奸。")
            logger.info(
                "PS5 list sent to group %s (database=%.1fms total=%.1fms)",
                group_id,
                database_ms,
                (time.perf_counter() - started) * 1000,
            )
            return
        entries = [
            ListCardEntry(
                name=item.nickname or item.online_id,
                current_game=item.current_game,
                avatar_url=item.avatar_url,
                game_image_url=item.current_game_image_url,
            )
            for item in items
        ]
        try:
            message = await build_list_message("ps", entries)
        except Exception:
            logger.exception("Failed to render PSN list card")
            message = Message(
                "\n".join(list_line(entry.name, entry.current_game) for entry in entries)
            )
        await bot.send(event, message)
        logger.info(
            "PS5 list sent to group %s (items=%d database=%.1fms total=%.1fms)",
            group_id,
            len(items),
            database_ms,
            (time.perf_counter() - started) * 1000,
        )
        return

    if action in {"nickname", "昵称"}:
        if len(parts) != 3:
            await bot.send(event, HELP)
            return
        try:
            online_id = normalize_online_id(parts[1])
            nickname = normalize_nickname(parts[2])
        except ValueError as exc:
            await bot.send(event, str(exc))
            return
        updated = await asyncio.to_thread(
            registry.set_nickname, online_id, group_id, user_id, nickname
        )
        await bot.send(
            event,
            f"昵称已改为 {nickname}。"
            if updated
            else "本群没有找到这条登记。",
        )
        return

    if action not in {"add", "remove"} or (
        action == "remove" and len(parts) != 2
    ) or (action == "add" and len(parts) not in {2, 3}):
        await bot.send(event, HELP)
        return
    try:
        online_id = normalize_online_id(parts[1])
    except ValueError as exc:
        await bot.send(event, str(exc))
        return
    nickname = None
    if action == "add" and len(parts) == 3:
        try:
            nickname = normalize_nickname(parts[2])
        except ValueError as exc:
            await bot.send(event, str(exc))
            return

    if action == "remove":
        removed = await asyncio.to_thread(registry.remove, online_id, group_id, user_id)
        if removed:
            await bot.send(event, f"已取消视奸 {online_id}。")
        else:
            await bot.send(event, "本群没有找到这条登记。")
        return

    existing = await asyncio.to_thread(registry.list_group, group_id)
    if any(item.online_id.casefold() == online_id.casefold() for item in existing):
        await bot.send(event, "这个 PSN 在线 ID 已经在本群视奸列表中。")
        return

    if not ps5_client.configured:
        await bot.send(event, "PSN 观察账号尚未登录，请联系机器人管理员。")
        return

    try:
        user = await ps5_client.lookup(online_id)
        all_items = await asyncio.to_thread(registry.list_all)
        known = next((item for item in all_items if item.account_id == str(user["account_id"])), None)
        item = Ps5Subscription(
            online_id=str(user["online_id"]),
            account_id=str(user["account_id"]),
            avatar_url=str(user["avatar_url"]) if user.get("avatar_url") else None,
            qq_user_id=user_id,
            group_id=group_id,
            status="active",
            current_game=None,
            initialised=False,
            force_notify=known is not None,
            nickname=nickname,
        )
        await asyncio.to_thread(registry.add, item)
    except Ps5VisibilityError:
        message = MessageSegment.at(event.user_id) + MessageSegment.text(
            " 请把“在线状态和当前游戏”设为所有人可见"
        )
        await bot.send(event, message)
        return
    except Ps5Error as exc:
        await bot.send(event, str(exc))
        return
    except sqlite3.IntegrityError:
        await bot.send(event, "这个 PSN 在线 ID 已经登记。")
        return
    except Exception:
        logger.exception("Unexpected error while adding PSN user %s", online_id)
        await bot.send(event, "发送失败：PSN 服务暂时无法完成操作，请稍后再试。")
        return

    avatar = await small_image_segment(item.avatar_url, 96, 96)
    message = Message()
    if avatar:
        message += avatar
    message += MessageSegment.text("已加入本群 PS5 视奸列表。")
    await bot.send(event, message)

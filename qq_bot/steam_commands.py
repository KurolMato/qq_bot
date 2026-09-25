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
from .steam_registry import (
    SteamError,
    SteamSubscription,
    SteamVisibilityError,
    normalize_steam_identifier,
    registry,
    steam_client,
)
from .switch_presence import small_image_segment


config = BotConfig.from_env()
logger = logging.getLogger(__name__)
steam_command = on_command("steam", priority=10, block=True)
HELP = (
    "Steam 视奸命令：\n"
    "/steam add 好友码/SteamID64/个人主页链接/自定义ID [昵称]  登记公开账号\n"
    "/steam list  查看本群视奸列表\n"
    "/steam nickname SteamID64或名称 昵称  修改昵称\n"
    "/steam remove SteamID64或名称  取消本群视奸\n"
    "/steam status  检查 Steam 服务"
)


@steam_command.handle()
async def handle_steam_command(bot: Bot, event: GroupMessageEvent, argument: Message = CommandArg()) -> None:
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
        if not steam_client.configured:
            await bot.send(event, "Steam Web API Key 尚未配置，请在机器人电脑的管理器中选择“配置 Steam API Key”。")
            return
        try:
            await steam_client.status()
            await bot.send(event, "Steam 服务正常。")
        except SteamError as exc:
            await bot.send(event, str(exc))
        return
    if action == "list":
        started = time.perf_counter()
        items = registry.list_group(group_id)
        if not items:
            await bot.send(event, "本群还没有登记 Steam 视奸。")
            return
        entries = [
            ListCardEntry(
                name=item.nickname or item.display_name,
                current_game=item.current_game,
                avatar_url=item.avatar_url,
                game_image_url=item.current_game_image_url,
            )
            for item in items
        ]
        try:
            message = await build_list_message("steam", entries)
        except Exception:
            logger.exception("Failed to render Steam list card")
            message = Message(
                "\n".join(list_line(entry.name, entry.current_game) for entry in entries)
            )
        await bot.send(event, message)
        logger.info("Steam list sent to group %s (items=%d total=%.1fms)", group_id, len(items), (time.perf_counter() - started) * 1000)
        return
    if action in {"nickname", "昵称"}:
        if len(parts) != 3:
            await bot.send(event, HELP)
            return
        try:
            nickname = normalize_nickname(parts[2])
        except ValueError as exc:
            await bot.send(event, str(exc))
            return
        identifier = parts[1].strip().rstrip("/")
        updated = await asyncio.to_thread(
            registry.set_nickname, identifier, group_id, user_id, nickname
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

    if action == "remove":
        identifier = parts[1].strip().rstrip("/")
        removed = await asyncio.to_thread(registry.remove, identifier, group_id, user_id)
        await bot.send(
            event,
            f"已取消视奸 {identifier}。" if removed else "本群没有找到这条登记。",
        )
        return

    try:
        identifier = normalize_steam_identifier(parts[1])
    except ValueError as exc:
        await bot.send(event, str(exc))
        return
    nickname = None
    if len(parts) == 3:
        try:
            nickname = normalize_nickname(parts[2])
        except ValueError as exc:
            await bot.send(event, str(exc))
            return
    if not steam_client.configured:
        await bot.send(event, "Steam Web API Key 尚未配置，请联系机器人管理员。")
        return
    try:
        player = await steam_client.lookup(parts[1])
        steam_id = str(player["steamid"])
        existing = await asyncio.to_thread(registry.list_group, group_id)
        if any(item.steam_id == steam_id for item in existing):
            await bot.send(event, "这个 Steam 用户已经在本群视奸列表中。")
            return
        all_items = await asyncio.to_thread(registry.list_all)
        item = SteamSubscription(
            steam_id=steam_id,
            display_name=str(player.get("personaname") or steam_id),
            identifier=identifier,
            avatar_url=str(player.get("avatarfull") or "") or None,
            qq_user_id=user_id,
            group_id=group_id,
            status="active",
            current_game=None,
            initialised=False,
            force_notify=any(entry.steam_id == steam_id for entry in all_items),
            nickname=nickname,
        )
        await asyncio.to_thread(registry.add, item)
    except SteamVisibilityError:
        await bot.send(
            event,
            MessageSegment.at(event.user_id)
            + MessageSegment.text(" 请把 Steam“我的个人资料”和“游戏详情”设为公开"),
        )
        return
    except SteamError as exc:
        await bot.send(event, str(exc))
        return
    except sqlite3.IntegrityError:
        await bot.send(event, "这个 Steam 用户已经登记。")
        return
    except Exception:
        logger.exception("Unexpected error while adding Steam user %s", identifier)
        await bot.send(event, "Steam 服务暂时无法完成操作，请稍后再试。")
        return

    avatar = await small_image_segment(item.avatar_url, 96, 96)
    message = Message()
    if avatar:
        message += avatar
    message += MessageSegment.text("已加入本群 Steam 视奸列表。")
    await bot.send(event, message)

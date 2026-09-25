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
from .switch_presence import small_image_segment
from .xbox_registry import XboxError, XboxSubscription, XboxVisibilityError, normalize_gamertag, registry, xbox_client


config = BotConfig.from_env()
logger = logging.getLogger(__name__)
xbox_command = on_command("xbox", priority=10, block=True)
HELP = (
    "Xbox 视奸命令：\n"
    "/xbox add 玩家代号 [昵称]  登记公开状态\n"
    "/xbox list  查看本群视奸列表\n"
    "/xbox nickname 玩家代号 昵称  修改昵称\n"
    "/xbox remove 玩家代号  取消本群视奸\n"
    "/xbox status  检查 Xbox 服务"
)


@xbox_command.handle()
async def handle_xbox_command(bot: Bot, event: GroupMessageEvent, argument: Message = CommandArg()) -> None:
    group_id, user_id = str(event.group_id), str(event.user_id)
    if config.allowed_groups and group_id not in config.allowed_groups:
        return
    parts = argument.extract_plain_text().strip().split(maxsplit=2)
    action = parts[0].lower() if parts else "help"
    if action in {"help", "帮助", "?"}:
        await bot.send(event, HELP)
        return
    if action == "status":
        if not xbox_client.configured:
            await bot.send(event, "Xbox 观察账号尚未登录，请在管理后台打开 Xbox 登录。")
            return
        try:
            await xbox_client.status()
            await bot.send(event, f"Xbox 服务正常（{xbox_client.provider_name}）。")
        except XboxError as exc:
            await bot.send(event, str(exc))
        return
    if action == "list":
        started = time.perf_counter()
        items = registry.list_group(group_id)
        if not items:
            await bot.send(event, "本群还没有登记 Xbox 视奸。")
            return
        entries = [ListCardEntry(item.nickname or item.gamertag, item.current_game, item.avatar_url, item.current_game_image_url) for item in items]
        try:
            message = await build_list_message("xbox", entries)
        except Exception:
            logger.exception("Failed to render Xbox list card")
            message = Message("\n".join(list_line(entry.name, entry.current_game) for entry in entries))
        await bot.send(event, message)
        logger.info("Xbox list sent to group %s (items=%d total=%.1fms)", group_id, len(items), (time.perf_counter() - started) * 1000)
        return
    if action in {"nickname", "昵称"}:
        if len(parts) != 3:
            await bot.send(event, HELP)
            return
        try:
            gamertag = normalize_gamertag(parts[1])
            nickname = normalize_nickname(parts[2])
        except ValueError as exc:
            await bot.send(event, str(exc))
            return
        updated = await asyncio.to_thread(registry.set_nickname, gamertag, group_id, user_id, nickname)
        await bot.send(event, f"昵称已改为 {nickname}。" if updated else "本群没有找到这条登记。")
        return
    if action not in {"add", "remove"} or (
        action == "remove" and len(parts) != 2
    ) or (action == "add" and len(parts) not in {2, 3}):
        await bot.send(event, HELP)
        return
    try:
        gamertag = normalize_gamertag(parts[1])
        nickname = normalize_nickname(parts[2]) if action == "add" and len(parts) == 3 else None
    except ValueError as exc:
        await bot.send(event, str(exc))
        return
    if action == "remove":
        removed = await asyncio.to_thread(registry.remove, gamertag, group_id, user_id)
        await bot.send(event, f"已取消视奸 {gamertag}。" if removed else "本群没有找到这条登记。")
        return
    if any(item.gamertag.casefold() == gamertag.casefold() for item in registry.list_group(group_id)):
        await bot.send(event, "这个 Xbox 玩家代号已经在本群视奸列表中。")
        return
    if not xbox_client.configured:
        await bot.send(event, "Xbox 观察账号尚未登录，请在管理后台打开 Xbox 登录。")
        return
    try:
        user = await xbox_client.lookup(gamertag)
        all_items = await asyncio.to_thread(registry.list_all)
        known = next((item for item in all_items if item.xuid == str(user["xuid"])), None)
        item = XboxSubscription(
            gamertag=str(user["gamertag"]), xuid=str(user["xuid"]),
            avatar_url=str(user["avatar_url"]) if user.get("avatar_url") else None,
            qq_user_id=user_id, group_id=group_id, force_notify=known is not None,
            nickname=nickname,
        )
        await asyncio.to_thread(registry.add, item)
    except XboxVisibilityError:
        await bot.send(event, MessageSegment.at(event.user_id) + MessageSegment.text(" 请把 Xbox 的在线状态和当前游戏设为所有人可见"))
        return
    except XboxError as exc:
        await bot.send(event, str(exc))
        return
    except sqlite3.IntegrityError:
        await bot.send(event, "这个 Xbox 玩家代号已经登记。")
        return
    except Exception:
        logger.exception("Unexpected error while adding Xbox user %s", gamertag)
        await bot.send(event, "Xbox 服务暂时无法完成操作，请稍后再试。")
        return
    avatar = await small_image_segment(item.avatar_url, 96, 96)
    message = Message()
    if avatar:
        message += avatar
    message += MessageSegment.text("已加入本群 Xbox 视奸列表。")
    await bot.send(event, message)

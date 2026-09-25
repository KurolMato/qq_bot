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
from .switch_registry import (
    FRIEND_REQUEST_PENDING_MESSAGE,
    NxapiError,
    SwitchSubscription,
    normalize_friend_code,
    nxapi_client,
    registry,
)
from .switch_presence import small_image_segment


config = BotConfig.from_env()
logger = logging.getLogger(__name__)
switch_command = on_command("switch", priority=10, block=True)
HELP = (
    "Switch 视奸命令：\n"
    "/switch add SW-1234-5678-9012 [昵称]  发送好友申请并登记\n"
    "/switch list  查看本群视奸列表\n"
    "/switch nickname SW-1234-5678-9012 昵称  修改昵称\n"
    "/switch remove SW-1234-5678-9012  取消本群视奸\n"
    "/switch status  检查 nxapi 状态"
)


def _command_reply(message_id: int, content: str | Message | MessageSegment) -> Message:
    message = Message(MessageSegment.reply(message_id))
    message += content
    return message


@switch_command.handle()
async def handle_switch_command(bot: Bot, event: GroupMessageEvent, argument: Message = CommandArg()) -> None:
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
        if not nxapi_client.available:
            await bot.send(event, "nxapi 未安装。请在机器人电脑上按《使用说明》完成安装与登录。")
            return
        try:
            friends = await nxapi_client.friends(retries=1)
            await bot.send(event, f"nxapi 已登录，观察账号当前有 {len(friends)} 位 Switch 好友。")
        except NxapiError as exc:
            await bot.send(event, f"nxapi 尚未就绪：{exc}")
        except Exception:
            logger.exception("Unexpected Switch status error")
            await bot.send(event, "Switch 状态暂时无法查询，请稍后再试。")
        return

    if action == "list":
        started = time.perf_counter()
        items = registry.list_group(group_id)
        database_ms = (time.perf_counter() - started) * 1000
        if not items:
            await bot.send_group_msg(group_id=event.group_id, message="本群还没有登记 Switch 视奸。")
            logger.info(
                "Switch list sent to group %s (database=%.1fms total=%.1fms)",
                group_id,
                database_ms,
                (time.perf_counter() - started) * 1000,
            )
            return
        entries = [
            ListCardEntry(
                name=item.nickname or item.ns_name,
                current_game=item.current_game,
                avatar_url=item.avatar_url,
                game_image_url=item.current_game_image_url,
            )
            for item in items
        ]
        try:
            message = await build_list_message("switch", entries)
        except Exception:
            logger.exception("Failed to render Switch list card")
            message = Message(
                "\n".join(list_line(entry.name, entry.current_game) for entry in entries)
            )
        await bot.send_group_msg(group_id=event.group_id, message=message)
        logger.info(
            "Switch list sent to group %s (items=%d database=%.1fms total=%.1fms)",
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
            friend_code = normalize_friend_code(parts[1])
            nickname = normalize_nickname(parts[2])
        except ValueError as exc:
            await bot.send(event, str(exc))
            return
        updated = await asyncio.to_thread(
            registry.set_nickname, friend_code, group_id, user_id, nickname
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
        friend_code = normalize_friend_code(parts[1])
    except ValueError as exc:
        response = _command_reply(event.message_id, str(exc)) if action == "add" else str(exc)
        await bot.send(event, response)
        return
    nickname = None
    if action == "add" and len(parts) == 3:
        try:
            nickname = normalize_nickname(parts[2])
        except ValueError as exc:
            await bot.send(event, _command_reply(event.message_id, str(exc)))
            return

    if action == "remove":
        removed = await asyncio.to_thread(
            registry.remove, friend_code, group_id, user_id
        )
        if removed:
            await bot.send(event, f"已取消视奸 {friend_code}。注意：这不会自动删除 NS 好友。")
        else:
            await bot.send(event, "本群没有找到这条登记。")
        return

    existing = await asyncio.to_thread(registry.list_group, group_id)
    if any(item.friend_code == friend_code for item in existing):
        await bot.send(event, _command_reply(event.message_id, "这个好友码已经在本群视奸列表中。"))
        return
    all_items = await asyncio.to_thread(registry.list_all)
    known = next((item for item in all_items if item.friend_code == friend_code), None)
    if known:
        item = SwitchSubscription(
            friend_code=known.friend_code,
            nsa_id=known.nsa_id,
            ns_name=known.ns_name,
            avatar_url=known.avatar_url,
            qq_user_id=user_id,
            group_id=group_id,
            status=known.status,
            current_game=None,
            initialised=False,
            force_notify=True,
            nickname=nickname,
        )
        try:
            await asyncio.to_thread(registry.add, item)
        except sqlite3.IntegrityError:
            await bot.send(
                event,
                _command_reply(event.message_id, "这个好友码已经在本群视奸列表中。"),
            )
            return
        avatar = await small_image_segment(item.avatar_url, 96, 96)
        message = Message()
        if avatar:
            message += avatar
        message += MessageSegment.text("已加入本群视奸列表，无需重复发送好友申请。")
        await bot.send(event, _command_reply(event.message_id, message))
        return

    try:
        user = await nxapi_client.lookup(friend_code)
        friends = await nxapi_client.friends(retries=2)
        already_friend = any(str(friend.get("nsaId")) == str(user["nsaId"]) for friend in friends)
        result = "already friends"
        if not already_friend:
            await bot.send(event, _command_reply(event.message_id, "正在发送好友申请…"))
            try:
                result = await nxapi_client.add_friend(friend_code)
            except NxapiError as exc:
                # A previous attempt may have sent the request but failed before
                # the SQLite record was written. Treat Nintendo's duplicate
                # request response as a pending success so /switch add can heal it.
                if str(exc) != FRIEND_REQUEST_PENDING_MESSAGE:
                    raise
                result = "friend request sent"
        item = SwitchSubscription(
            friend_code=friend_code,
            nsa_id=str(user["nsaId"]),
            ns_name=str(user.get("name") or "Switch 玩家"),
            avatar_url=str(user.get("imageUri")) if user.get("imageUri") else None,
            qq_user_id=user_id,
            group_id=group_id,
            status="active" if already_friend or "now friends" in result.lower() else "pending",
            current_game=None,
            initialised=False,
            nickname=nickname,
        )
        await asyncio.to_thread(registry.add, item)
    except NxapiError as exc:
        await bot.send(event, _command_reply(event.message_id, f"发送失败：{exc}"))
        return
    except sqlite3.IntegrityError:
        await bot.send(event, _command_reply(event.message_id, "这个好友码已经登记。"))
        return
    except Exception:
        logger.exception("Unexpected error while adding Switch friend code %s", friend_code)
        await bot.send(
            event,
            _command_reply(event.message_id, "发送失败：Switch 服务暂时无法完成操作，请稍后再试。"),
        )
        return

    avatar = await small_image_segment(item.avatar_url, 96, 96)
    message = Message()
    if avatar:
        message += avatar
    if already_friend:
        message += MessageSegment.text("好友已存在，已加入本群视奸列表。")
    else:
        message += MessageSegment.text("好友申请已发送，请在 Switch 上同意。")
    await bot.send(
        event,
        _command_reply(event.message_id, message),
    )

from __future__ import annotations

import asyncio
import logging

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, PrivateMessageEvent
from nonebot.adapters.onebot.v11.exception import ActionFailed, NetworkError
from nonebot.params import CommandArg

from .config import BotConfig
from .game_time_leaderboard import build_leaderboard_message
from .game_time_tracker import tracker
from .monthly_rank_cache import monthly_cache
from .personal_game_time import PERIODS, parse_personal_query, resolve_binding_name
from .personal_rank import build_personal_rank_message
from .hot_games import build_hot_games_message


logger = logging.getLogger(__name__)
_pending_personal_profiles: set[tuple[str, str, str]] = set()


async def _send_reply(bot: Bot, event: GroupMessageEvent, message: str | Message) -> None:
    try:
        await bot.send(event, message)
    except (NetworkError, ActionFailed) as exc:
        # A missing acknowledgement does not mean QQ did not deliver the message.
        # Retrying or sending a fallback can duplicate an already delivered reply.
        logger.warning(
            "Game-time reply delivery unconfirmed for group %s: %s; "
            "not retrying to avoid duplicates. Check NapCat connection and send logs.",
            event.group_id,
            exc,
        )


config = BotConfig.from_env()
hot_command = on_command("hot", priority=10, block=True)
_pending_hot: set[tuple[str, str, str | None]] = set()
_HOT_USAGE = "用法：/hot 本周热游；/hot m 本月热游（按游玩人数排名）"


@hot_command.handle()
async def handle_hot_command(bot: Bot, event: GroupMessageEvent,
                             argument: Message = CommandArg()) -> None:
    group_id = str(event.group_id)
    if config.allowed_groups and group_id not in config.allowed_groups:
        return
    text = argument.extract_plain_text().strip().casefold()
    if text not in {"", "m"}:
        await _send_reply(bot, event, _HOT_USAGE)
        return
    period = "month" if text == "m" else "week"
    private_user = str(event.user_id) if getattr(event, "message_type", None) == "private" else None
    key = (group_id, period, private_user)
    if key in _pending_hot:
        return
    _pending_hot.add(key)
    try:
        try:
            reply = await build_hot_games_message(tracker, group_id, period)
        except Exception:
            logger.exception("Hot games query failed for group %s", group_id)
            reply = "群热游生成失败，请稍后再试。"
        await _send_reply(bot, event, reply)
    finally:
        _pending_hot.discard(key)


@hot_command.handle()
async def handle_private_hot_command(
    bot: Bot, event: PrivateMessageEvent, argument: Message = CommandArg(),
) -> None:
    from . import dsh_commands
    from .project_images import ImageRequest, private_image_request

    if not dsh_commands._enabled() or not dsh_commands.is_allowed(event.user_id):
        return
    text = argument.extract_plain_text().strip().casefold()
    if text not in {"", "m"}:
        await bot.send(event, _HOT_USAGE)
        return
    await private_image_request(bot, event, "", request=ImageRequest("hot", text))


rank_command = on_command(
    "rank",
    aliases={"排行", "游戏排行", "今日排行", "排", "排名", "排行榜"},
    priority=10,
    block=True,
)


@rank_command.handle()
async def handle_rank_command(
    bot: Bot,
    event: GroupMessageEvent,
    argument: Message = CommandArg(),
) -> None:
    group_id = str(event.group_id)
    if config.allowed_groups and group_id not in config.allowed_groups:
        return
    text = argument.extract_plain_text().strip()
    other_board = False
    if text.casefold() == "o" or text.casefold().startswith("o "):
        other_board = True
        text = text[1:].strip()
    action, _, game_name = text.partition(" ")
    action = action.casefold()
    if action in {"我", "我的"}:
        action = "me"
    game_name = " ".join(game_name.strip().split())
    if action in {"me", "player"}:
        profile_month = (int(game_name)
                         if len(game_name) <= 2 and game_name.isascii() and game_name.isdecimal() else None)
        is_profile = action == "me" and (not game_name or profile_month is not None)
        pending_key = (group_id, str(event.user_id), str(profile_month)) if is_profile else None
        if pending_key in _pending_personal_profiles:
            return
        if pending_key is not None:
            _pending_personal_profiles.add(pending_key)
        try:
            try:
                if action == "player":
                    period, member_name = parse_personal_query(game_name)
                else:
                    if (profile_month is not None and not 1 <= profile_month <= 12) or (
                        game_name and profile_month is None and game_name.casefold() not in PERIODS
                    ):
                        raise ValueError("用法：/rank me [d/y/w/m 或 1-12月份]，例如 /rank me 9")
                    period = PERIODS.get(game_name.casefold(), "day")
                    member_name = await asyncio.to_thread(tracker.bound_member, group_id, str(event.user_id))
                    if member_name is None:
                        raise ValueError("请先发送 /绑定 昵称或玩家名，再使用 /rank me 查询。")
                if is_profile:
                    if profile_month is None:
                        reply = await build_personal_rank_message(tracker, group_id, member_name)
                    else:
                        reply = await build_personal_rank_message(
                            tracker, group_id, member_name, month=profile_month,
                        )
                else:
                    result = await asyncio.to_thread(
                        tracker.personal_snapshot, group_id, member_name, period=period
                    )
                    reply = await build_leaderboard_message(result, personal_name=member_name)
            except ValueError as exc:
                reply = str(exc)
            except Exception:
                logger.exception("Personal game-time query failed for group %s", group_id)
                reply = "个人时长查询失败，请稍后再试。"
            await _send_reply(bot, event, reply)
        finally:
            if pending_key is not None:
                _pending_personal_profiles.discard(pending_key)
        return
    if other_board and action in {"move", "restore"}:
        if not game_name:
            await _send_reply(bot, event, f"用法：/rank o {action} 成员名")
            return
        try:
            display_name, changed = await asyncio.to_thread(
                tracker.move_member_to_other_rank if action == "move" else tracker.restore_member_to_main_rank,
                group_id,
                game_name,
            )
        except ValueError as exc:
            await _send_reply(bot, event, str(exc))
            return
        if action == "move":
            message = (
                f"已将 {display_name} 移至 /rank o。"
                if changed
                else f"{display_name} 已经在 /rank o 中。"
            )
        else:
            message = (
                f"已将 {display_name} 恢复至 /rank。"
                if changed
                else f"{display_name} 当前不在 /rank o 中。"
            )
        await _send_reply(bot, event, message)
        return
    if action in {"remove", "add"}:
        if not game_name:
            await _send_reply(bot, event, f"用法：/rank {action} 游戏名")
            return
        try:
            changed = await asyncio.to_thread(
                tracker.hide_game if action == "remove" else tracker.unhide_game,
                group_id,
                game_name,
            )
        except ValueError:
            await _send_reply(bot, event, f"用法：/rank {action} 游戏名")
            return
        if action == "remove":
            await _send_reply(
                bot, event,
                f"已屏蔽排行榜游戏：{game_name}。实时游玩播报不受影响。"
                if changed
                else f"排行榜已经屏蔽：{game_name}。",
            )
        else:
            await _send_reply(
                bot, event,
                f"已取消排行榜屏蔽：{game_name}。"
                if changed
                else f"排行榜当前未屏蔽：{game_name}。",
            )
        return
    month_number: int | None = None
    monthly = action == "m"
    weekly = action == "w"
    yesterday = action == "y"
    if monthly and game_name:
        if not game_name.isdigit() or not 1 <= int(game_name) <= 12:
            await _send_reply(bot, event, "用法：/rank m [月份]，例如 /rank m 8")
            return
        month_number = int(game_name)
    if (weekly or yesterday) and game_name:
        await _send_reply(bot, event, f"用法：/rank {'o ' if other_board else ''}{action}")
        return
    if text and not monthly and not weekly and not yesterday:
        await _send_reply(
            bot, event,
            "用法：/rank 查看今日排行（也可用 /排、/排名、/排行榜）\n/rank y 查看昨日结算\n/rank w 查看本周排行\n/rank m 查看月排行\n"
            "/rank o 查看另一成员组（同样支持 y、w、m）\n"
            "/绑定 昵称或玩家名\n/rank me 个人档案（也可用 /我的排名、/排名我）\n/rank me 月份 月末结算档案（例如 /rank me 9）\n/rank me [d/y/w/m] 个人时长明细\n/rank player [d/y/w/m] 昵称 指定成员时长\n"
            "/rank o move 成员名 移入另一成员组\n/rank o restore 成员名 恢复至主榜\n"
            "/rank remove 游戏名 屏蔽游戏\n/rank add 游戏名 取消屏蔽",
        )
        return
    if monthly:
        try:
            message = await monthly_cache.message(
                group_id, month=month_number, board="other" if other_board else "main",
            )
        except Exception:
            logger.exception("Monthly rank cache unavailable for group %s", group_id)
            return
        await _send_reply(bot, event, message)
        return
    if yesterday:
        snapshot = await asyncio.to_thread(
            tracker.yesterday_snapshot, group_id, board="other" if other_board else "main"
        )
    elif weekly:
        snapshot = await asyncio.to_thread(
            tracker.weekly_snapshot, group_id, board="other" if other_board else "main"
        )
    else:
        snapshot = await asyncio.to_thread(
            tracker.snapshot, group_id, board="other" if other_board else "main"
        )
    if not snapshot.members:
        if yesterday:
            await _send_reply(bot, event, f"{snapshot.day_start:%m月%d日}没有可统计的游戏时长。")
        elif weekly:
            await _send_reply(bot, event, "本周还没有可统计的游戏时长。")
        else:
            await _send_reply(bot, event, "今天还没有可统计的游戏时长。")
        return
    try:
        message = await build_leaderboard_message(snapshot)
    except Exception:
        logger.exception("Failed to render game-time leaderboard for group %s", group_id)
        await _send_reply(bot, event, "排行榜生成失败，请稍后再试。")
        return
    await _send_reply(bot, event, message)


rank_me_command = on_command(
    "我的排名",
    aliases={"排名我", "我的排行"},
    priority=10,
    block=True,
)


@rank_me_command.handle()
async def handle_rank_me_command(
    bot: Bot,
    event: GroupMessageEvent,
    argument: Message = CommandArg(),
) -> None:
    extra = argument.extract_plain_text().strip()
    await handle_rank_command(bot, event, Message(f"me {extra}".strip()))


bind_command = on_command("绑定", priority=10, block=True)


@bind_command.handle()
async def handle_bind_command(
    bot: Bot, event: GroupMessageEvent, argument: Message = CommandArg(),
) -> None:
    group_id = str(event.group_id)
    if config.allowed_groups and group_id not in config.allowed_groups:
        return
    try:
        name = await asyncio.to_thread(resolve_binding_name, group_id, argument.extract_plain_text().strip())
        await asyncio.to_thread(tracker.bind_qq_member, group_id, str(event.user_id), name)
        reply = f"已绑定：{name}。"
    except ValueError as exc:
        reply = str(exc)
    except Exception:
        logger.exception("Member binding failed for group %s", group_id)
        reply = "绑定暂时失败，请稍后再试。"
    await _send_reply(bot, event, reply)

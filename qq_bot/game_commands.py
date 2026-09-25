from __future__ import annotations

import asyncio
import logging
import re
from datetime import date

from nonebot import on_command, on_regex
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message
from nonebot.params import CommandArg
from nonebot.rule import to_me
from .game_progress import ProgressService, build_progress_messages
from .steam_registry import steam_client

from .config import BotConfig
from .game_calendar import (
    GameCalendarRegistry,
    GameLookupError,
    SteamGameMatcher,
    build_calendar_message,
    build_year_calendar_message,
    calendar_year_months,
    parse_month_selector,
)
from .health import snapshot
from .list_cards import ListCardEntry, build_online_overview_message
from .ps5_registry import registry as ps5_registry
from .steam_registry import registry as steam_registry
from .store_matchers import (
    NintendoStoreMatcher,
    PlayStationStoreMatcher,
    is_nintendo_store_url,
    is_playstation_store_url,
)
from .switch_registry import registry as switch_registry
from .xbox_registry import registry as xbox_registry


logger = logging.getLogger(__name__)
config = BotConfig.from_env()
registry = GameCalendarRegistry()
steam_matcher = SteamGameMatcher()
nintendo_matcher = NintendoStoreMatcher()
playstation_matcher = PlayStationStoreMatcher()
game_command = on_command("game", priority=10, block=True)
mentioned_game_command = on_regex(r"(?i)^game(?:\s|$)", rule=to_me(), priority=9, block=True)
progress_service = ProgressService(steam_client, steam_registry)
_progress_groups: set[str] = set()


async def _online_platform_entries(group_id: str) -> dict[str, list[ListCardEntry]]:
    steam_items, ps_items, switch_items, xbox_items = await asyncio.gather(
        asyncio.to_thread(steam_registry.list_group, group_id),
        asyncio.to_thread(ps5_registry.list_group, group_id),
        asyncio.to_thread(switch_registry.list_group, group_id),
        asyncio.to_thread(xbox_registry.list_group, group_id),
    )
    return {
        "steam": [
            ListCardEntry(
                item.nickname or item.display_name,
                item.current_game,
                item.avatar_url,
                item.current_game_image_url,
            )
            for item in steam_items
            if item.current_game
        ],
        "ps": [
            ListCardEntry(
                item.nickname or item.online_id,
                item.current_game,
                item.avatar_url,
                item.current_game_image_url,
            )
            for item in ps_items
            if item.current_game
        ],
        "switch": [
            ListCardEntry(
                item.nickname or item.ns_name,
                item.current_game,
                item.avatar_url,
                item.current_game_image_url,
            )
            for item in switch_items
            if item.current_game
        ],
        "xbox": [
            ListCardEntry(
                item.nickname or item.gamertag,
                item.current_game,
                item.avatar_url,
                item.current_game_image_url,
            )
            for item in xbox_items
            if item.current_game
        ],
    }


def _select_matcher(value: str):
    if is_nintendo_store_url(value):
        return nintendo_matcher, value
    if is_playstation_store_url(value):
        return playstation_matcher, value
    platform, separator, query = value.partition(" ")
    if separator:
        key = platform.casefold()
        if key in {"switch", "ns"}:
            return nintendo_matcher, query.strip()
        if key in {"psn", "ps", "ps5"}:
            return playstation_matcher, query.strip()
        if key == "steam":
            return steam_matcher, query.strip()
    return steam_matcher, value


@game_command.handle()
async def handle_game_command(
    bot: Bot,
    event: GroupMessageEvent,
    argument: Message = CommandArg(),
) -> None:
    if config.allowed_groups and str(event.group_id) not in config.allowed_groups:
        return
    text = argument.extract_plain_text().strip()
    action, _, value = text.partition(" ")
    action = action.casefold()
    value = value.strip()

    if action == "add":
        if not value:
            await bot.send(event, "用法：/game add 游戏名（也支持 switch/psn 前缀或商店链接）")
            return
        try:
            selected_matcher, query = _select_matcher(value)
            if not query:
                await bot.send(event, "请在平台名称后输入游戏名。")
                return
            game = await selected_matcher.lookup(query)
            created, saved_game = await asyncio.to_thread(
                registry.add_resolved, game, str(event.user_id)
            )
        except GameLookupError as exc:
            await bot.send(event, str(exc))
            return
        except Exception:
            logger.exception("Unexpected game calendar lookup failure for %s", value)
            await bot.send(event, "游戏资料查询失败，请稍后再试。")
            return
        verb = "游戏已添加" if created else "游戏已更新"
        await bot.send(
            event,
            f"{verb}：{saved_game.name} {saved_game.release_date:%Y-%m-%d}",
        )
        return

    if action == "list":
        if re.fullmatch(r"20\d{2}", value):
            year = int(value)
            months = calendar_year_months(year)
            try:
                games = await asyncio.to_thread(registry.list_all)
                games = [game for game in games if game.release_date.year == year and game.release_date.month in months]
                message = await build_year_calendar_message(year, months, games)
            except Exception:
                logger.exception("Failed to render game release calendars for %d", year)
                await bot.send(event, "游戏发售日历生成失败，请稍后再试。")
                return
            await bot.send(event, message)
            return
        try:
            year, month = parse_month_selector(value or str(date.today().month))
        except ValueError:
            await bot.send(event, "用法：/game list 9、/game list 2027-9，或 /game list 2026（横向拼接月表）")
            return
        games = await asyncio.to_thread(registry.list_month, year, month)
        if not games:
            await bot.send(event, f"{year}年{month}月还没有登记游戏。")
            return
        try:
            message = await build_calendar_message(year, month, games)
        except Exception:
            logger.exception("Failed to render game release calendar for %d-%02d", year, month)
            await bot.send(event, "游戏发售日历生成失败，请稍后再试。")
            return
        await bot.send(event, message)
        return

    if action == "ol" and not value:
        groups = await _online_platform_entries(str(event.group_id))
        health_names = {"steam": "steam", "ps": "ps5", "switch": "switch", "xbox": "xbox"}
        unhealthy_platforms = {
            platform
            for platform, component in health_names.items()
            if snapshot(component).state == "error"
        }
        if not any(groups.values()) and not unhealthy_platforms:
            await bot.send(event, "当前四个平台都没有正在游玩的玩家。")
            return
        try:
            message = await build_online_overview_message(groups, unhealthy_platforms)
        except Exception:
            logger.exception("Failed to render four-platform online overview")
            await bot.send(event, "在线玩家列表生成失败，请稍后再试。")
            return
        await bot.send(event, message)
        return

    if text and action not in {"help", "帮助"}:
        group_id = str(event.group_id)
        if group_id in _progress_groups:
            await bot.send(event, "本群正在查询游戏进度，请稍候。")
            return
        _progress_groups.add(group_id)
        try:
            try:
                result = await progress_service.query(group_id, text)
            except GameLookupError as exc:
                await bot.send(event, str(exc))
                return
            except Exception:
                logger.exception("Steam group progress query failed")
                await bot.send(event, "Steam 游戏进度查询失败或超时，请稍后重试。")
                return
            reply = Message()
            async for message in build_progress_messages(result):
                reply += message
            await bot.send(event, reply)
        finally:
            _progress_groups.discard(group_id)
        return

    await bot.send(
        event,
        "可用命令：\n"
        "/game 游戏名或AppID  查看本群 Steam 成就进度（支持 @机器人 game 游戏名）\n"
        "/game add 游戏名（Steam）\n"
        "/game add switch 游戏名\n"
        "/game add psn 游戏名\n"
        "/game add 商店链接\n"
        "/game list 月份\n"
        "/game list 年份  横向拼接月表（当年从本月开始）\n"
        "/game ol  查看四平台正在游玩的玩家",
    )


@mentioned_game_command.handle()
async def handle_mentioned_game(bot: Bot, event: GroupMessageEvent) -> None:
    await handle_game_command(bot, event, Message(event.get_plaintext().strip()[4:].strip()))

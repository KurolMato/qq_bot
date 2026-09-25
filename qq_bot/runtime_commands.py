from __future__ import annotations

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message
from nonebot.params import CommandArg

from .config import BotConfig
from .health import ComponentHealth, snapshot, uptime_text


config = BotConfig.from_env()
runtime_command = on_command("bot", priority=10, block=True)


def _line(label: str, health: ComponentHealth) -> str:
    icon = {"ok": "正常", "error": "异常", "disabled": "未启用", "starting": "启动中"}.get(
        health.state,
        "未知",
    )
    detail = ("已连接" if label == "QQ / NapCat" else "正常") if health.state == "ok" else health.detail
    return f"{label}：{icon}（{detail}）"


@runtime_command.handle()
async def handle_runtime_command(bot: Bot, event: GroupMessageEvent, argument: Message = CommandArg()) -> None:
    group_id = str(event.group_id)
    if config.allowed_groups and group_id not in config.allowed_groups:
        return
    action = argument.extract_plain_text().strip().lower() or "status"
    if action not in {"status", "状态"}:
        await bot.send(event, "可用命令：/bot status")
        return
    lines = [
        "机器人运行状态：",
        _line("QQ / NapCat", snapshot("qq")),
        _line("视频解析", snapshot("video")),
        _line("Switch", snapshot("switch")),
        _line("PS5", snapshot("ps5")),
        _line("Steam", snapshot("steam")),
        _line("Xbox", snapshot("xbox")),
        f"运行时间：{uptime_text()}",
    ]
    await bot.send(event, "\n".join(lines))

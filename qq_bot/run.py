from __future__ import annotations

import os
import logging
from pathlib import Path

import nonebot
from nonebot.adapters.onebot.v11 import Adapter
from nonebot.log import LoguruHandler, logger
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

from .switch_presence import start_switch_monitor, stop_switch_monitor
from .switch_registry import start_nxapi_friend_monitor, stop_nxapi_friend_monitor
from .ps5_registry import start_ps5_monitor, stop_ps5_monitor
from .steam_registry import start_steam_monitor, stop_steam_monitor
from .xbox_registry import start_xbox_monitor, stop_xbox_monitor
from .watchdog import start_watchdog, stop_watchdog
from .steam_achievements import start_achievement_monitor, stop_achievement_monitor
from .monthly_rank_cache import start_monthly_rank_cache, stop_monthly_rank_cache
from .game_release_reminder import (
    start_game_release_reminder,
    stop_game_release_reminder,
)


class _UvicornLogConfigFilter(logging.Filter):
    """Avoid duplicating records that Uvicorn already routes through loguru."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not record.name.casefold().startswith("uvicorn.")


def _install_logging_bridge() -> None:
    """Route stdlib logging records through NoneBot's loguru sinks.

    The bot modules intentionally use ``logging.getLogger`` while NoneBot
    writes through loguru. Installing one root bridge keeps those records in
    ``data/qq-bot.log`` and prevents the stdlib last-resort stderr handler from
    swallowing them when the bot is launched without a visible console.
    """

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # A launcher or an embedding process may have configured a stream/file
    # handler already. Remove root handlers before installing the single
    # bridge, otherwise every stdlib record is printed twice.
    for handler in list(root.handlers):
        root.removeHandler(handler)
    bridge = LoguruHandler()
    # The FastAPI driver installs its own LoguruHandler for uvicorn.error and
    # uvicorn.access. Filtering those names here keeps the root bridge from
    # emitting a second copy once Uvicorn starts.
    bridge.addFilter(_UvicornLogConfigFilter())
    root.addHandler(bridge)
    # INFO request URLs can include the Steam API key in the query string.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def main() -> None:
    if os.name == "nt":
        from .napcat_watchdog import _single_instance
        import hashlib
        identity = hashlib.sha256(str(PROJECT_ROOT.resolve()).casefold().encode()).hexdigest()[:16]
        if not _single_instance("Local\\VideoAnalysisQQBot-" + identity):
            logger.error("This project's QQ Bot is already running; refusing duplicate heartbeat writer")
            return
    (PROJECT_ROOT / "data").mkdir(parents=True, exist_ok=True)
    logger.add(
        PROJECT_ROOT / "data" / "qq-bot.log",
        rotation="2 MB",
        retention=3,
        encoding="utf-8",
        enqueue=True,
    )
    _install_logging_bridge()
    nonebot.init(
        host=os.getenv("QQ_BOT_HOST", "127.0.0.1"),
        port=int(os.getenv("QQ_BOT_PORT", "8081")),
    )
    driver = nonebot.get_driver()
    driver.register_adapter(Adapter)
    driver.on_startup(start_switch_monitor)
    driver.on_shutdown(stop_switch_monitor)
    driver.on_startup(start_nxapi_friend_monitor)
    driver.on_shutdown(stop_nxapi_friend_monitor)
    driver.on_startup(start_ps5_monitor)
    driver.on_shutdown(stop_ps5_monitor)
    driver.on_startup(start_steam_monitor)
    driver.on_shutdown(stop_steam_monitor)
    driver.on_startup(start_achievement_monitor)
    driver.on_shutdown(stop_achievement_monitor)
    driver.on_startup(start_xbox_monitor)
    driver.on_shutdown(stop_xbox_monitor)
    driver.on_startup(start_watchdog)
    driver.on_shutdown(stop_watchdog)
    driver.on_startup(start_game_release_reminder)
    driver.on_shutdown(stop_game_release_reminder)
    driver.on_startup(start_monthly_rank_cache)
    driver.on_shutdown(stop_monthly_rank_cache)
    nonebot.load_plugin("qq_bot.plugin")
    nonebot.load_plugin("qq_bot.switch_commands")
    nonebot.load_plugin("qq_bot.ps5_commands")
    nonebot.load_plugin("qq_bot.steam_commands")
    nonebot.load_plugin("qq_bot.xbox_commands")
    nonebot.load_plugin("qq_bot.runtime_commands")
    nonebot.load_plugin("qq_bot.game_time_commands")
    nonebot.load_plugin("qq_bot.game_commands")
    nonebot.load_plugin("qq_bot.hltb_commands")
    nonebot.load_plugin("qq_bot.help_message")
    nonebot.run()


if __name__ == "__main__":
    main()

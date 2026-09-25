from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta

import nonebot

from .config import BotConfig
from .game_calendar import (
    CHINA_TIMEZONE,
    GameCalendarRegistry,
    build_release_reminder_message,
)
from .resilience import supervise, wait_for_stop


logger = logging.getLogger(__name__)
config = BotConfig.from_env()
registry = GameCalendarRegistry()


def _integer_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        logger.warning("Invalid %s; using %d", name, default)
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        logger.warning("Invalid %s; using %.0f", name, default)
        return default


def _reminder_days() -> tuple[int, ...]:
    values: set[int] = set()
    for value in os.getenv("GAME_RELEASE_REMINDER_DAYS", "1,0").split(","):
        try:
            day = int(value.strip())
        except ValueError:
            continue
        if 0 <= day <= 30:
            values.add(day)
    return tuple(sorted(values, reverse=True)) or (1, 0)


class GameReleaseReminder:
    def __init__(
        self,
        calendar: GameCalendarRegistry,
        groups: frozenset[str],
        *,
        hour: int | None = None,
        lead_days: tuple[int, ...] | None = None,
        interval: float | None = None,
    ) -> None:
        self.calendar = calendar
        self.groups = groups
        configured_hour = _integer_env("GAME_RELEASE_REMINDER_HOUR", 9)
        self.hour = max(0, min(hour if hour is not None else configured_hour, 23))
        self.lead_days = lead_days if lead_days is not None else _reminder_days()
        self.interval = max(
            interval if interval is not None else _float_env("GAME_RELEASE_REMINDER_INTERVAL", 300),
            60.0,
        )
        self._stop = asyncio.Event()

    async def stop(self) -> None:
        self._stop.set()

    async def poll_once(self, now: datetime | None = None) -> None:
        local_now = (now or datetime.now(CHINA_TIMEZONE)).astimezone(CHINA_TIMEZONE)
        if local_now.hour < self.hour or not self.groups:
            return
        bots = list(nonebot.get_bots().values())
        if not bots:
            return
        bot = bots[0]
        today = local_now.date()
        for lead_days in self.lead_days:
            release_date = today + timedelta(days=lead_days)
            games = await asyncio.to_thread(self.calendar.list_date, release_date)
            if not games:
                continue
            pending_groups = [
                group_id
                for group_id in sorted(self.groups)
                if not await asyncio.to_thread(
                    self.calendar.reminder_was_sent,
                    group_id,
                    release_date,
                    lead_days,
                )
            ]
            if not pending_groups:
                continue
            try:
                message = await build_release_reminder_message(
                    release_date, lead_days, games
                )
            except Exception:
                logger.exception(
                    "Failed to render release reminder for %s", release_date
                )
                continue
            for group_id in pending_groups:
                try:
                    await asyncio.wait_for(
                        bot.send_group_msg(group_id=int(group_id), message=message),
                        timeout=20,
                    )
                    await asyncio.to_thread(
                        self.calendar.mark_reminder_sent,
                        group_id,
                        release_date,
                        lead_days,
                    )
                    logger.info(
                        "Sent game release reminder to group %s for %s (lead=%d)",
                        group_id,
                        release_date,
                        lead_days,
                    )
                except TimeoutError:
                    logger.warning(
                        "Game release reminder timed out for group %s", group_id
                    )
                except Exception:
                    logger.exception(
                        "Failed to send game release reminder to group %s", group_id
                    )

    async def run(self) -> None:
        if not self.groups:
            logger.warning(
                "Game release reminder disabled because QQ_ALLOWED_GROUPS is empty"
            )
        while not self._stop.is_set():
            await self.poll_once()
            await wait_for_stop(self._stop, self.interval)


_monitor: GameReleaseReminder | None = None
_task: asyncio.Task[None] | None = None


async def start_game_release_reminder() -> None:
    global _monitor, _task
    _monitor = GameReleaseReminder(registry, config.allowed_groups)
    _task = asyncio.create_task(
        supervise("game-release-reminder", _monitor.run, _monitor._stop.is_set),
        name="game-release-reminder-supervisor",
    )
    logger.info(
        "Game release reminder ready (groups=%d hour=%d days=%s)",
        len(_monitor.groups),
        _monitor.hour,
        _monitor.lead_days,
    )


async def stop_game_release_reminder() -> None:
    global _monitor, _task
    if _monitor:
        await _monitor.stop()
    if _task:
        await _task
    _monitor = None
    _task = None

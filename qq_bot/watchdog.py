from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

import httpx
import nonebot

from .config import BotConfig
from .atomic_json import write_json
from .health import mark_failure, mark_success, snapshot, uptime_text
from .resilience import FailureBackoff, supervise, wait_for_stop


logger = logging.getLogger(__name__)
config = BotConfig.from_env()
PROJECT_ROOT = Path(__file__).resolve().parent.parent
HEARTBEAT_PATH = PROJECT_ROOT / "data" / "qq-runtime-heartbeat.json"


def _write_heartbeat(*, connected: bool) -> None:
    components = {}
    for name in ("qq", "video", "switch", "ps5", "steam", "xbox"):
        health = snapshot(name)
        components[name] = {
            "state": health.state,
            "detail": health.detail,
            "failures": health.failures,
        }
    HEARTBEAT_PATH.parent.mkdir(parents=True, exist_ok=True)
    write_json(HEARTBEAT_PATH,
            {
                "version": 1,
                "updated_at": time.time(),
                "pid": os.getpid(),
                "onebot_connected": connected,
                "components": components,
                "uptime": uptime_text(),
            },
    )


class RuntimeWatchdog:
    def __init__(self) -> None:
        self.interval = max(float(os.getenv("BOT_HEALTH_INTERVAL", "30")), 10.0)
        self.heartbeat_interval = max(float(os.getenv("BOT_HEARTBEAT_INTERVAL", "5")), 3.0)
        self._stop = asyncio.Event()
        self._last_write_error = float("-inf")

    async def write_heartbeat(self, connected: bool) -> None:
        try:
            await asyncio.to_thread(_write_heartbeat, connected=connected)
        except OSError:
            now = time.monotonic()
            if now - self._last_write_error >= 60:
                logger.warning("Heartbeat file update failed after retries; keeping previous heartbeat", exc_info=True)
                self._last_write_error = now
        else:
            if self._last_write_error != float("-inf"):
                logger.info("Heartbeat file updates recovered")
                self._last_write_error = float("-inf")

    async def stop(self) -> None:
        self._stop.set()
        await self.write_heartbeat(False)

    async def run(self) -> None:
        backoff = FailureBackoff(self.interval, 300.0)
        timeout = httpx.Timeout(5.0, connect=3.0)
        next_video_check = 0.0
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            while not self._stop.is_set():
                connected = bool(nonebot.get_bots())
                await self.write_heartbeat(connected)
                if connected:
                    mark_success("qq", "已连接")
                else:
                    mark_failure("qq", "等待 NapCat 连接")

                now = time.monotonic()
                if now >= next_video_check:
                    try:
                        response = await client.get(f"{config.api_url}/health")
                        response.raise_for_status()
                        mark_success("video", "正常")
                        backoff.success()
                        video_delay = self.interval
                    except (httpx.HTTPError, ValueError) as exc:
                        mark_failure("video", "暂时不可用")
                        video_delay = backoff.failure()
                        if backoff.should_log():
                            logger.warning(
                                "Video API health check failed: %s; retry in %.0fs",
                                type(exc).__name__,
                                video_delay,
                            )
                    next_video_check = now + video_delay

                await wait_for_stop(self._stop, self.heartbeat_interval)


_watchdog: RuntimeWatchdog | None = None
_task: asyncio.Task[None] | None = None


async def start_watchdog() -> None:
    global _watchdog, _task
    _watchdog = RuntimeWatchdog()
    _task = asyncio.create_task(
        supervise("runtime-watchdog", _watchdog.run, _watchdog._stop.is_set),
        name="runtime-watchdog-supervisor",
    )


async def stop_watchdog() -> None:
    global _watchdog, _task
    if _watchdog:
        await _watchdog.stop()
    if _task:
        await _task
    _watchdog = None
    _task = None

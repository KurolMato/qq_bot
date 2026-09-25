from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass


logger = logging.getLogger(__name__)


@dataclass
class FailureBackoff:
    base_delay: float
    max_delay: float
    log_interval: float = 300.0
    failures: int = 0
    _last_log: float = 0.0

    def success(self) -> None:
        self.failures = 0
        self._last_log = 0.0

    def failure(self) -> float:
        self.failures += 1
        return min(self.base_delay * (2 ** (self.failures - 1)), self.max_delay)

    def should_log(self, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        if self._last_log == 0.0 or current - self._last_log >= self.log_interval:
            self._last_log = current
            return True
        return False


async def wait_for_stop(stop: asyncio.Event, delay: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=max(delay, 0.0))
    except TimeoutError:
        pass


async def supervise(
    name: str,
    worker: Callable[[], Awaitable[None]],
    should_stop: Callable[[], bool],
) -> None:
    """Restart a background loop if an unforeseen exception makes it exit."""
    failures = FailureBackoff(2.0, 60.0, 60.0)
    while not should_stop():
        try:
            await worker()
            if should_stop():
                return
            raise RuntimeError("background worker returned unexpectedly")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            delay = failures.failure()
            if failures.should_log():
                logger.error("Background task %s stopped unexpectedly: %s; restart in %.0fs", name, exc, delay)
            await asyncio.sleep(delay)

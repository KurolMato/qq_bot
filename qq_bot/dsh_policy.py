"""Small reply and scheduling policy helpers for the DSH bridge (no NoneBot imports)."""

from __future__ import annotations

import random
from datetime import datetime


def in_quiet_window(now: datetime, start_hour: int, end_hour: int) -> bool:
    """True inside the allowed window; supports windows that cross midnight."""
    hour = now.hour
    if start_hour <= end_hour:
        return start_hour <= hour < end_hour
    return hour >= start_hour or hour < end_hour


def proactive_allowed(
    now: datetime,
    idle_seconds: float,
    day_count: int,
    *,
    daily_max: int,
    quiet_start: int,
    quiet_end: int,
    idle_minutes: int,
) -> bool:
    """Whether it is a good moment to message the owner unprompted."""
    if daily_max <= 0 or day_count >= daily_max:
        return False
    if not in_quiet_window(now, quiet_start, quiet_end):
        return False
    return idle_seconds >= idle_minutes * 60


STICKER_SEND_PROBABILITY = 0.33


def should_send_sticker() -> bool:
    """Apply the shared send chance once per ordinary text reply."""
    return random.random() < STICKER_SEND_PROBABILITY

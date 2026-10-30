from __future__ import annotations

from datetime import datetime

from qq_bot.dsh_policy import in_quiet_window, proactive_allowed


def _at(hour: int) -> datetime:
    return datetime(2026, 10, 26, hour, 0, 0)


def test_quiet_window_daytime() -> None:
    assert in_quiet_window(_at(10), 9, 23) is True
    assert in_quiet_window(_at(8), 9, 23) is False
    assert in_quiet_window(_at(23), 9, 23) is False


def test_quiet_window_crossing_midnight() -> None:
    assert in_quiet_window(_at(23), 22, 7) is True
    assert in_quiet_window(_at(3), 22, 7) is True
    assert in_quiet_window(_at(12), 22, 7) is False


def test_proactive_requires_idle_gap() -> None:
    kwargs = dict(daily_max=3, quiet_start=9, quiet_end=23, idle_minutes=30)
    assert proactive_allowed(_at(12), 29 * 60, 0, **kwargs) is False
    assert proactive_allowed(_at(12), 31 * 60, 0, **kwargs) is True


def test_proactive_respects_daily_cap_and_quiet_hours() -> None:
    kwargs = dict(daily_max=2, quiet_start=9, quiet_end=23, idle_minutes=30)
    assert proactive_allowed(_at(12), 3600, 2, **kwargs) is False
    assert proactive_allowed(_at(2), 3600, 0, **kwargs) is False


def test_proactive_disabled_by_zero_cap() -> None:
    assert proactive_allowed(_at(12), 3600, 0, daily_max=0, quiet_start=9, quiet_end=23, idle_minutes=30) is False

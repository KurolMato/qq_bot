from __future__ import annotations

import asyncio
from datetime import date, datetime

from qq_bot.game_calendar import CHINA_TIMEZONE, CalendarGame
from qq_bot.game_release_reminder import GameReleaseReminder


class FakeCalendar:
    def __init__(self) -> None:
        self.sent: set[tuple[str, date, int]] = set()
        self.target = date(2026, 9, 6)
        self.game = CalendarGame("1", "Example Game", self.target, None)

    def list_date(self, value: date):
        return [self.game] if value == self.target else []

    def reminder_was_sent(self, group_id: str, value: date, lead_days: int) -> bool:
        return (group_id, value, lead_days) in self.sent

    def mark_reminder_sent(self, group_id: str, value: date, lead_days: int) -> None:
        self.sent.add((group_id, value, lead_days))


class FakeBot:
    def __init__(self) -> None:
        self.messages: list[tuple[int, object]] = []

    async def send_group_msg(self, *, group_id: int, message: object) -> None:
        self.messages.append((group_id, message))


def test_release_reminder_sends_once_per_group_and_lead_day(monkeypatch) -> None:
    from qq_bot import game_release_reminder as module

    calendar = FakeCalendar()
    bot = FakeBot()

    async def build(_release_date, _lead_days, _games):
        return "reminder-card"

    monkeypatch.setattr(module.nonebot, "get_bots", lambda: {"bot": bot})
    monkeypatch.setattr(module, "build_release_reminder_message", build)
    reminder = GameReleaseReminder(
        calendar,  # type: ignore[arg-type]
        frozenset({"100", "200"}),
        hour=9,
        lead_days=(1,),
    )
    now = datetime(2026, 9, 5, 9, 30, tzinfo=CHINA_TIMEZONE)

    asyncio.run(reminder.poll_once(now))
    asyncio.run(reminder.poll_once(now))

    assert bot.messages == [(100, "reminder-card"), (200, "reminder-card")]
    assert calendar.sent == {
        ("100", date(2026, 9, 6), 1),
        ("200", date(2026, 9, 6), 1),
    }


def test_release_reminder_waits_until_configured_hour(monkeypatch) -> None:
    from qq_bot import game_release_reminder as module

    calendar = FakeCalendar()
    bot = FakeBot()
    monkeypatch.setattr(module.nonebot, "get_bots", lambda: {"bot": bot})
    reminder = GameReleaseReminder(
        calendar,  # type: ignore[arg-type]
        frozenset({"100"}),
        hour=9,
        lead_days=(1,),
    )

    asyncio.run(
        reminder.poll_once(datetime(2026, 9, 5, 8, 59, tzinfo=CHINA_TIMEZONE))
    )

    assert bot.messages == []
    assert calendar.sent == set()

import asyncio
from datetime import datetime, timezone
from io import BytesIO
from unittest.mock import AsyncMock

import pytest
from PIL import Image, ImageDraw

from qq_bot import game_time_leaderboard as board
from qq_bot.game_time_tracker import LeaderboardSnapshot, TrackedGame, TrackedMember


def _snapshot(period):
    return LeaderboardSnapshot(
        datetime(2026, 9, 1, tzinfo=timezone.utc), None,
        (
            TrackedMember("First", None, 10799, (
                TrackedGame("steam", "ExactHour", 3600, "hidden-cover"),
                TrackedGame("steam", "UnderHour", 3599, None),
                TrackedGame("ps", "AnotherHour", 3600, None),
            )),
            TrackedMember("Second", None, 7201, (
                TrackedGame("steam", "VisibleGame", 3601, "visible-cover"),
                TrackedGame("steam", "BoundaryGame", 3600, None),
            )),
        ), period,
    )


@pytest.mark.parametrize("period,personal,filtered", [
    ("month", None, True), ("month", "First", False),
    ("day", None, False), ("week", None, False), ("yesterday", None, False),
])
def test_display_filter_preserves_totals_and_order(monkeypatch, period, personal, filtered):
    texts = []
    original = ImageDraw.ImageDraw.text

    def capture(self, xy, text, *args, **kwargs):
        texts.append(text)
        return original(self, xy, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", capture)
    snapshot = _snapshot(period)
    content = board._render(snapshot, {}, personal)
    assert not any('仅展示超过1小时' in text for text in texts)
    assert texts.index("First") < texts.index("Second")
    assert "3小时" in texts and "2小时" in texts
    assert "VisibleGame" in texts
    assert ("ExactHour" not in texts) == filtered
    assert ("UnderHour" not in texts) == filtered
    assert ("BoundaryGame" not in texts) == filtered
    assert ("暂无超过1小时的游戏" in texts) == filtered
    assert len(snapshot.members[0].games) == 3
    with Image.open(BytesIO(content)) as image:
        assert image.height == (580 if filtered else 740)


def test_monthly_assets_skip_hidden_game_covers(monkeypatch):
    assets = AsyncMock(return_value={})
    monkeypatch.setattr(board, "_load_assets", assets)
    monkeypatch.setattr(board, "_render", lambda *args: b"mock image")
    monkeypatch.setattr(board, "_prepare_image", lambda content: content)
    asyncio.run(board.build_leaderboard_message(_snapshot("month")))
    entries = assets.await_args.args[0]
    assert len(entries) == 2
    assert entries[0].current_game is None
    assert entries[0].game_image_url is None
    assert entries[1].game_image_url == "visible-cover"

from datetime import datetime, timezone
from io import BytesIO
from unittest.mock import AsyncMock
import asyncio

import pytest
from PIL import Image, ImageDraw
from qq_bot import game_time_leaderboard as board
from qq_bot.game_time_tracker import LeaderboardSnapshot, TrackedMember, TrackedGame


def fixture(period='day', games=None, name='Player'):
    games = games if games is not None else tuple(TrackedGame(platform, platform, seconds, platform)
        for platform, seconds in [('steam', 7200), ('ps5', 3600), ('switch', 1800), ('xbox', 900)])
    return LeaderboardSnapshot(datetime(2026, 11, 1, tzinfo=timezone.utc), None,
        (TrackedMember(name, None, sum(g.seconds for g in games), games),), period)


def test_platform_badges_and_per_player_relative_bars(monkeypatch):
    marks = []
    original = ImageDraw.ImageDraw.rounded_rectangle
    def record(self, xy, *args, **kwargs):
        marks.append((xy, kwargs.get('fill')))
        return original(self, xy, *args, **kwargs)
    monkeypatch.setattr(ImageDraw.ImageDraw, 'rounded_rectangle', record)
    board._render(fixture(), {})
    for platform, fraction in [('steam', 1), ('ps5', .5), ('switch', .25), ('xbox', .125)]:
        color = board.PLATFORM[platform][1]
        shapes = [box for box, fill in marks if fill == color]
        assert any(box[2] - box[0] == 72 and box[3] - box[1] == 26 for box in shapes)
        assert any(box[2] - box[0] == round(244 * fraction) and box[3] - box[1] == 4 for box in shapes)


@pytest.mark.parametrize('period', ['day', 'week', 'month', 'yesterday'])
def test_all_periods_handle_long_labels_zero_time_and_broken_art(period):
    games = tuple(TrackedGame('steam', '很长的游戏名称' * 15, 7201 if i else 0, 'bad') for i in range(8))
    snapshot = fixture(period, games, '很长的玩家昵称' * 15)
    with Image.open(BytesIO(board._render(snapshot, {'bad': b'bad'}))) as image:
        assert image.width == 900
        assert image.height > 700
    empty = LeaderboardSnapshot(snapshot.day_start, None, (), period)
    with Image.open(BytesIO(board._render(empty, {}))) as image:
        assert image.height == 300


def test_cover_loading_includes_every_visible_game(monkeypatch):
    loader = AsyncMock(return_value={})
    monkeypatch.setattr(board, '_load_assets', loader)
    monkeypatch.setattr(board, '_render', lambda *a: b'fixture')
    asyncio.run(board.build_leaderboard_image(fixture()))
    assert [e.game_image_url for e in loader.await_args.args[0]] == ['steam', 'ps5', 'switch', 'xbox']

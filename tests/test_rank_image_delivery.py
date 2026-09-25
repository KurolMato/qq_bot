import asyncio
import base64
from datetime import datetime, timezone
from io import BytesIO
import random
from unittest.mock import AsyncMock

from PIL import Image

from qq_bot import game_time_leaderboard as board
from qq_bot.game_time_tracker import LeaderboardSnapshot, TrackedGame, TrackedMember


def _png(image):
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def test_small_image_unchanged():
    content = _png(Image.new("RGB", (900, 500), "navy"))
    assert board._prepare_image(content) == content


def test_over_five_mb_image_becomes_one_bounded_image():
    source = Image.frombytes("RGB", (900, 2400), random.Random(42).randbytes(900 * 2400 * 3))
    content = _png(source)
    assert len(content) > 5_000_000
    encoded = board._prepare_image(content)
    assert len(encoded) <= board.MAX_IMAGE_BYTES
    with Image.open(BytesIO(encoded)) as image:
        assert image.format == "JPEG"
        assert image.width <= source.width and image.height <= source.height
        assert abs(image.width / image.height - source.width / source.height) < 0.001


def test_jpeg_compression_preserves_dimensions_when_it_fits(monkeypatch):
    source = Image.effect_noise((900, 1000), 30).convert("RGB")
    content = _png(source)
    monkeypatch.setattr(board, "MAX_IMAGE_BYTES", len(content) - 1)
    with Image.open(BytesIO(board._prepare_image(content))) as image:
        assert image.format == "JPEG"
        assert image.size == source.size


def test_long_rank_sends_exactly_one_complete_image(monkeypatch):
    snapshot = LeaderboardSnapshot(datetime(2026, 9, 1, tzinfo=timezone.utc), None, tuple(
        TrackedMember(f"Player {index}", None, 7200,
                      (TrackedGame("steam", "Game", 7200, None),))
        for index in range(30)
    ), "month")
    monkeypatch.setattr(board, "_load_assets", AsyncMock(return_value={}))
    message = asyncio.run(board.build_leaderboard_message(snapshot))
    assert len(message) == 1 and message[0].type == "image"
    content = base64.b64decode(message[0].data["file"].removeprefix("base64://"))
    assert len(content) <= board.MAX_IMAGE_BYTES
    with Image.open(BytesIO(content)) as image:
        assert image.size == (900, 206 + 30 * 210)

import asyncio
import base64
from datetime import datetime, timezone
from io import BytesIO
from dataclasses import replace
from unittest.mock import AsyncMock

from PIL import Image
import pytest

from qq_bot import personal_rank as rank
from qq_bot.game_time_tracker import TrackedGame
from qq_bot.personal_rank_data import PersonalRankData


def data():
    return PersonalRankData("Tester", "failed-avatar", 600, 600, 600,
                            (TrackedGame("steam", "Missing cover", 600, "failed-cover"),),
                            (), None, None, {}, datetime(2026, 9, 25, tzinfo=timezone.utc))


def image_bytes(size=(640, 360)):
    output = BytesIO()
    Image.new('RGB', size, 'navy').save(output, format='PNG')
    return output.getvalue()


def test_dashboard_finishes_loading_before_render_and_sends_one_image(monkeypatch):
    profile = data()
    loaded_assets = {'failed-cover': image_bytes(), 'failed-avatar': image_bytes((100, 100))}
    finished = []
    monkeypatch.setattr(rank, "build_personal_rank_data", lambda *args: profile)

    async def load(value):
        await asyncio.sleep(0)
        finished.append("assets")
        return value, loaded_assets

    async def psn(value, gid, *, tracker=None):
        await asyncio.sleep(0)
        finished.append("psn")
        return value

    render = rank.render_personal_rank

    def checked_render(value, assets):
        assert set(finished) == {"assets", "psn"}
        assert assets == loaded_assets
        return render(value, assets)

    monkeypatch.setattr(rank, "load_personal_rank_assets", load)
    monkeypatch.setattr(rank, "enrich_personal_rank_psn", psn)
    monkeypatch.setattr(rank, "render_personal_rank", checked_render)
    message = asyncio.run(rank.build_personal_rank_message(object(), "100", "Tester"))
    assert len(message) == 1 and message[0].type == "image"
    content = base64.b64decode(message[0].data["file"].removeprefix("base64://"))
    with Image.open(BytesIO(content)) as output:
        assert output.width > 0 and output.height > 0
        output.load()
    assert len(content) <= 2_000_000


@pytest.mark.parametrize('content', [None, b'broken', image_bytes((100, 200))])
def test_missing_or_invalid_cover_never_reaches_renderer(monkeypatch, content):
    profile = replace(data(), avatar_url=None)
    monkeypatch.setattr(rank, 'build_personal_rank_data', lambda *args: profile)
    monkeypatch.setattr(rank, 'load_personal_rank_assets', AsyncMock(return_value=(profile, {'failed-cover': content})))
    monkeypatch.setattr(rank, 'enrich_personal_rank_psn', AsyncMock(return_value=profile))
    rendered = []
    monkeypatch.setattr(rank, 'render_personal_rank', lambda *args: rendered.append(args))
    async def run():
        task = asyncio.create_task(rank.build_personal_rank_message(object(), '100', 'Tester'))
        await asyncio.sleep(0.05)
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(run())
    assert rendered == []


def test_failed_avatar_blocks_send_and_unshown_games_do_not():
    games = tuple(TrackedGame('steam', str(i), 600, 'ready' if i < 6 else None) for i in range(7))
    profile = replace(data(), games=games)
    with pytest.raises(ValueError, match='头像暂未加载完成'):
        rank._require_loaded_images(profile, {'ready': image_bytes()})
    rank._require_loaded_images(replace(profile, avatar_url=None), {'ready': image_bytes()})


def test_switch_square_cover_does_not_wait_for_timeout():
    game = TrackedGame('switch', 'Nintendo game', 600, 'square')
    profile = replace(data(), avatar_url=None, games=(game,), recent_games=(game,))
    rank._require_loaded_images(profile, {'square': image_bytes((400, 400))})


def test_missing_recent_hero_blocks_send_even_if_month_collage_is_ready():
    profile = replace(data(), avatar_url=None, recent_games=(TrackedGame('ps', 'Weekly game', 1000, 'weekly'),))
    with pytest.raises(ValueError, match='横版封面暂未准备齐全'):
        rank._require_loaded_images(profile, {'failed-cover': image_bytes()})


def test_images_retry_automatically_then_return_one_image(monkeypatch):
    profile = replace(data(), avatar_url=None)
    loader = AsyncMock(side_effect=[(profile, {}), (profile, {'failed-cover': image_bytes()})])
    monkeypatch.setattr(rank, 'build_personal_rank_data', lambda *args: profile)
    monkeypatch.setattr(rank, 'load_personal_rank_assets', loader)
    monkeypatch.setattr(rank, 'enrich_personal_rank_psn', AsyncMock(return_value=profile))
    monkeypatch.setattr(rank, 'IMAGE_RETRY_INITIAL_SECONDS', 0.001)
    message = asyncio.run(rank.build_personal_rank_message(object(), '100', 'Tester'))
    assert loader.await_count == 2
    rank.enrich_personal_rank_psn.assert_awaited_once()
    assert len(message) == 1 and message[0].type == 'image'


@pytest.mark.parametrize('stalled', [False, True])
def test_total_deadline_sends_placeholders_and_preserves_loaded_cover(monkeypatch, stalled):
    ready = image_bytes()
    profile = replace(data(), avatar_url=None, games=(
        TrackedGame('ps', 'Ready', 600, 'ready'),
        TrackedGame('ps', 'Missing', 300, None),
    ), recent_games=(TrackedGame('ps', 'Missing', 300, None),))
    monkeypatch.setattr(rank, 'build_personal_rank_data', lambda *args: profile)
    monkeypatch.setattr(rank, 'enrich_personal_rank_psn', AsyncMock(return_value=profile))
    monkeypatch.setattr(rank, 'IMAGE_WAIT_TIMEOUT_SECONDS', 0.06)
    monkeypatch.setattr(rank, 'IMAGE_RETRY_INITIAL_SECONDS', 0.001)
    calls = 0
    cancelled = []

    async def loader(snapshot):
        nonlocal calls
        calls += 1
        if stalled and calls > 1:
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(True)
        return snapshot, {'ready': ready}

    monkeypatch.setattr(rank, 'load_personal_rank_assets', loader)
    captured = []

    def render(snapshot, assets):
        assert assets['ready'] == ready
        assert [g.name for g in snapshot.games] == ['Ready', 'Missing']
        assert snapshot.games[0].image_url == 'ready'
        assert snapshot.games[1].image_url.startswith('rank-timeout://')
        rank._require_loaded_images(snapshot, assets)
        captured.append(snapshot)
        return image_bytes()

    monkeypatch.setattr(rank, 'render_personal_rank', render)
    async def run():
        return await asyncio.wait_for(rank.build_personal_rank_message(object(), '100', 'Tester'), 1)
    message = asyncio.run(run())
    assert len(captured) == 1
    assert len(message) == 1 and message[0].type == 'image'
    assert bool(cancelled) == stalled


def test_first_batch_stall_still_sends_at_deadline(monkeypatch):
    profile = replace(data(), avatar_url=None)
    monkeypatch.setattr(rank, 'build_personal_rank_data', lambda *args: profile)
    monkeypatch.setattr(rank, 'enrich_personal_rank_psn', AsyncMock(return_value=profile))
    monkeypatch.setattr(rank, 'IMAGE_WAIT_TIMEOUT_SECONDS', 0.03)
    async def stalled(snapshot):
        await asyncio.Event().wait()
    monkeypatch.setattr(rank, 'load_personal_rank_assets', stalled)
    message = asyncio.run(rank.build_personal_rank_message(object(), '100', 'Tester'))
    assert len(message) == 1 and message[0].type == 'image'


def test_historical_dashboard_retries_with_fixed_month_end(monkeypatch):
    reference = datetime(2026, 9, 30, 19, 59, 59, 999999, tzinfo=timezone.utc)
    profile = replace(data(), avatar_url=None, as_of=reference, finalized_month=True)
    from unittest.mock import Mock
    builder = Mock(return_value=profile)
    monkeypatch.setattr(rank, 'personal_rank_month_end', lambda month: reference)
    monkeypatch.setattr(rank, 'build_personal_rank_data', builder)
    monkeypatch.setattr(rank, 'load_personal_rank_assets', AsyncMock(side_effect=[
        (profile, {}), (profile, {'failed-cover': image_bytes()}),
    ]))
    monkeypatch.setattr(rank, 'enrich_personal_rank_psn', AsyncMock(return_value=profile))
    monkeypatch.setattr(rank, 'IMAGE_RETRY_INITIAL_SECONDS', 0.001)
    source = object()
    message = asyncio.run(rank.build_personal_rank_message(source, '100', 'Tester', month=9))
    assert len(message) == 1 and message[0].type == 'image'
    assert builder.call_count == 2
    assert all(call.kwargs == {'now': reference, 'finalized_month': True} for call in builder.call_args_list)

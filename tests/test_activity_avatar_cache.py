import asyncio
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from PIL import Image

from qq_bot import list_cards as cache
from qq_bot import switch_presence as presence


def image_bytes(colour="blue"):
    output = BytesIO()
    Image.new("RGB", (16, 16), colour).save(output, "PNG")
    return output.getvalue()


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "avatars")


def test_success_refreshes_cached_avatar_and_failure_reuses_it(monkeypatch):
    old, new = image_bytes(), image_bytes("red")
    download = AsyncMock(side_effect=[old, new, httpx.ConnectError("offline")])
    monkeypatch.setattr(cache, "_download_image", download)
    for expected in (old, new, new):
        assert asyncio.run(cache._load_avatar_with_fallback("https://example.com/a", "steam:1")) == expected
    assert download.await_count == 3
    assert cache._cache_path("https://example.com/a").read_bytes() == new


@pytest.mark.parametrize("url", ["https://example.com/new", None])
def test_changed_or_missing_url_uses_same_accounts_last_avatar(monkeypatch, url):
    content = image_bytes()
    download = AsyncMock(return_value=content)
    monkeypatch.setattr(cache, "_download_image", download)
    asyncio.run(cache._load_avatar_with_fallback("https://example.com/old", "steam:1"))
    download.side_effect = httpx.ConnectError("offline")
    assert asyncio.run(cache._load_avatar_with_fallback(url, "steam:1")) == content
    assert asyncio.run(cache._load_avatar_with_fallback(url, "steam:2")) is None
    if url:
        # Do not associate the previous avatar with a failed new URL.
        assert not cache._cache_path(url).exists()


def test_existing_list_cache_is_reused_and_saved_for_account(monkeypatch):
    cache.CACHE_DIR.mkdir()
    content = image_bytes()
    cache._cache_path("https://example.com/list-avatar").write_bytes(content)
    monkeypatch.setattr(cache, "_download_image", AsyncMock(side_effect=httpx.ConnectError("offline")))
    assert asyncio.run(cache._load_avatar_with_fallback("https://example.com/list-avatar", "steam:1")) == content
    assert asyncio.run(cache._load_avatar_with_fallback("https://example.com/new", "steam:1")) == content


@pytest.mark.parametrize("result", [b"not an image", None])
def test_invalid_response_keeps_good_cache(monkeypatch, result):
    content = image_bytes()
    download = AsyncMock(return_value=content)
    monkeypatch.setattr(cache, "_download_image", download)
    asyncio.run(cache._load_avatar_with_fallback("https://example.com/a", "steam:1"))
    download.return_value = result
    assert asyncio.run(cache._load_avatar_with_fallback("https://example.com/a", "steam:1")) == content
    assert cache._cache_path("https://example.com/a").read_bytes() == content


def test_missing_or_corrupt_cache_returns_no_image(monkeypatch):
    cache.CACHE_DIR.mkdir()
    cache._cache_path("https://example.com/a").write_bytes(b"broken")
    cache._cache_path("activity-avatar:steam:1").write_bytes(b"broken")
    monkeypatch.setattr(cache, "_download_image", AsyncMock(side_effect=httpx.ConnectError("offline")))
    assert asyncio.run(cache._load_avatar_with_fallback("https://example.com/a", "steam:1")) is None
    assert asyncio.run(cache._load_avatar_with_fallback("https://example.com/b", "steam:2")) is None


def test_download_timeout_falls_back_before_notification_deadline(monkeypatch):
    cache.CACHE_DIR.mkdir()
    content = image_bytes()
    cache._cache_path("https://example.com/a").write_bytes(content)
    sleep, wait_for = asyncio.sleep, asyncio.wait_for
    cancelled = []

    async def stalled(_url):
        try:
            await sleep(60)
        finally:
            cancelled.append(True)

    async def short_wait(awaitable, *, timeout):
        assert timeout == 6
        return await wait_for(awaitable, timeout=.01)

    monkeypatch.setattr(cache, "_download_image", stalled)
    monkeypatch.setattr(cache.asyncio, "wait_for", short_wait)
    assert asyncio.run(cache._load_avatar_with_fallback("https://example.com/a", "steam:1")) == content
    assert cancelled == [True]


def test_cache_write_failure_still_returns_downloaded_avatar(monkeypatch):
    content = image_bytes()
    monkeypatch.setattr(cache, "_download_image", AsyncMock(return_value=content))
    monkeypatch.setattr(cache, "_save_cached_avatar", Mock(side_effect=PermissionError("read only")))
    assert asyncio.run(cache._load_avatar_with_fallback("https://example.com/a", "steam:1")) == content


def test_add_success_seeds_cache_for_later_activity_card(monkeypatch):
    content = image_bytes()
    download = AsyncMock(return_value=content)
    monkeypatch.setattr(cache, "_download_image", download)
    segment = asyncio.run(presence.small_image_segment("https://example.com/old", 96, 96, avatar_cache_key="steam:1"))
    assert segment.type == "image"
    download.side_effect = httpx.ConnectError("offline")
    cover = image_bytes("green")
    monkeypatch.setattr(presence, "_download_image", AsyncMock(return_value=cover))
    render = Mock(return_value=content)
    monkeypatch.setattr(presence, "_render_activity_card", render)
    activity = presence.SwitchActivity("Player", "PLAYING", "Game", "https://example.com/new", "https://example.com/cover", "Steam")
    message = asyncio.run(presence.build_activity_message("Player", activity, avatar_cache_key="steam:1"))
    assert message[0].type == "image"
    assert render.call_args.args[2:5] == (content, cover, "Steam")


def test_backup_cdn_success_is_cached_under_original_url_and_used_when_all_fail(monkeypatch):
    content = image_bytes()
    url = "https://avatars.steamstatic.com/88ca271e657a0882145b8764db8c4fdb7ea4c128_full.jpg"
    backup = url.replace("avatars.steamstatic.com", "avatars.akamai.steamstatic.com")
    download = AsyncMock(side_effect=[httpx.ConnectError("original reset"), content])
    monkeypatch.setattr(presence, "_download_image_single", download)
    assert asyncio.run(cache._load_avatar_with_fallback(url, "steam:1")) == content
    assert [call.args[0] for call in download.await_args_list] == [url, backup]
    assert cache._cache_path(url).read_bytes() == content
    download.reset_mock(side_effect=True)
    download.side_effect = httpx.ConnectError("all reset")
    assert asyncio.run(cache._load_avatar_with_fallback(url, "steam:1")) == content
    assert download.await_count == 3
    # The same cached URL is also available to the list card while offline.
    download.reset_mock()
    assert asyncio.run(cache._load_image(url, asyncio.Semaphore(1))) == content
    download.assert_not_awaited()


def test_steam_monitor_passes_stable_account_cache_key(monkeypatch):
    from qq_bot import steam_registry as steam
    from qq_bot import steam_achievements

    item = SimpleNamespace(steam_id="76561198000000000", group_id="200", nickname=None,
                           display_name="Player", avatar_url=None, current_game=None,
                           current_game_image_url=None, force_notify=False, initialised=False)
    registry = SimpleNamespace(list_all=lambda: [item], update_presence=Mock())
    client = SimpleNamespace(configured=True, players=AsyncMock(return_value={item.steam_id: {
        "steamid": item.steam_id, "gameid": "570", "gameextrainfo": "Game",
    }}), game_name=AsyncMock(return_value="Game"), game_cover=AsyncMock(return_value=None))
    class Bot:
        send_group_msg = AsyncMock()
    monkeypatch.setattr(steam, "Bot", Bot)
    monkeypatch.setattr(steam.nonebot, "get_bots", lambda: {"test": Bot()})
    monkeypatch.setattr(steam, "game_time_tracker", SimpleNamespace(observe=Mock(), mark_poll=Mock()))
    monkeypatch.setattr(steam_achievements, "observe_players", Mock())
    build = AsyncMock(return_value="card")
    monkeypatch.setattr(steam, "build_activity_message", build)
    asyncio.run(steam.SteamPresenceMonitor(registry, client).poll_once())
    assert build.await_args.kwargs == {"avatar_cache_key": f"steam:{item.steam_id}"}

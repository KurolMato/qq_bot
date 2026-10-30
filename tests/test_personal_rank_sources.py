import asyncio
import sys
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from qq_bot import personal_rank_sources as sources


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(sources, "_cache", {})
    monkeypatch.setattr(sources, "local_steam_app_id", lambda name: None)
    monkeypatch.setattr(sources, "_grid_urls", AsyncMock(return_value=()))


def test_steam_reads_store_art_fields_without_player_achievement_calls(monkeypatch):
    client = NS(game_details=AsyncMock(return_value={
        "header_image": "https://cdn/header.jpg", "capsule_image": "https://cdn/capsule.jpg",
    }))
    monkeypatch.setitem(sys.modules, "qq_bot.steam_registry", NS(SteamClient=lambda: client))
    game = NS(platform="steam", name="Game", image_url="https://shared.akamai.steamstatic.com/apps/123/library_600x900.jpg")
    assert asyncio.run(sources.native_cover_urls(game)) == ("https://cdn/header.jpg", "https://cdn/capsule.jpg")
    client.game_details.assert_awaited_once_with("123")
    asyncio.run(sources.native_cover_urls(game))
    assert client.game_details.await_count == 1


@pytest.mark.parametrize("platform,expected", [("ps", "ps"), ("switch", "switch")])
def test_reuses_calendar_store_matchers(monkeypatch, platform, expected):
    ps = NS(lookup=AsyncMock(return_value=NS(name="Game (英语, 中文)", image_url="ps")))
    switch = NS(lookup=AsyncMock(return_value=NS(name="Game", image_url="switch")))
    monkeypatch.setitem(sys.modules, "qq_bot.store_matchers", NS(
        PlayStationStoreMatcher=lambda: ps, NintendoStoreMatcher=lambda: switch,
    ))
    monkeypatch.setitem(sys.modules, "qq_bot.personal_rank_ps_store", NS(ps_store_cover_urls=AsyncMock(return_value=('ps',))))
    assert asyncio.run(sources.native_cover_urls(NS(platform=platform, name="Game", image_url=None))) == (expected,)


def test_fuzzy_search_result_cannot_replace_game_with_sequel(monkeypatch):
    client = NS(_public_get=AsyncMock(return_value={"items": [{"id": 2, "type": "app", "name": "Game 2"}]}))
    monkeypatch.setitem(sys.modules, "qq_bot.steam_registry", NS(SteamClient=lambda: client))
    assert asyncio.run(sources.native_cover_urls(NS(platform="steam", name="Game", image_url=None))) == ()


def test_cancelled_lookup_is_not_swallowed(monkeypatch):
    monkeypatch.setattr(sources, "_lookup", AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(sources.native_cover_urls(NS(platform="steam", name="Game", image_url=None)))


def test_bilingual_labels_keep_sequels_distinct():
    assert sources.same_title("七龍珠Z 卡卡洛特", "七龙珠Z 卡卡洛特 PS4&PS5 (简体中文, 繁体中文)")
    assert not sources.same_title("七龍珠Z 卡卡洛特", "七龙珠Z 卡卡洛特 体验版")
    assert sources.same_title("Splatoon 3（斯普拉遁 3）", "Splatoon 3")
    assert sources.same_title("The Blood of Dawnwalker", "The Blood of Dawnwalker 黎明行者之血")
    assert not sources.same_title("Super Mario Odyssey", "Super Mario Odyssey 64")
    assert not sources.same_title("Game", "Game 2")
    assert sources.same_title("SHADOW OF THE COLOSSUS™", "Shadow of the Colossus")


def test_trails_regional_spelling_keeps_sequels_and_editions_distinct():
    assert sources.same_title("空之軌跡 the 2nd", "空之轨迹 the 2nd")
    assert sources.same_title("空之軌跡 the 2nd (繁體中文)", "空之轨迹 the 2nd")
    for other in ("空之轨迹 the 1st", "空之轨迹 the 2nd 体验版",
                  "空之轨迹 the 2nd Nintendo Switch 2 Edition", "空之轨迹 SC"):
        assert not sources.same_title("空之軌跡 the 2nd", other)


def test_regional_names_share_successful_discovery_cache(monkeypatch):
    lookup = AsyncMock(return_value=("https://ps/banner.jpg",))
    monkeypatch.setattr(sources, "_lookup", lookup)
    for name in ("空之轨迹 the 2nd", "空之軌跡 the 2nd"):
        assert asyncio.run(sources.native_cover_urls(NS(platform="ps", name=name, image_url="same"))) == ("https://ps/banner.jpg",)
    lookup.assert_awaited_once()


def test_ps_public_store_precedes_grid(monkeypatch):
    grid = AsyncMock(return_value=("https://cdn/grid.jpg",))
    store = AsyncMock(return_value=("https://ps/banner.jpg",))
    monkeypatch.setattr(sources, "_grid_urls", grid)
    monkeypatch.setattr(sources, "_lookup", store)
    assert asyncio.run(sources.native_cover_urls(NS(platform="ps",name="Viewfinder",image_url=None))) == ("https://ps/banner.jpg",)
    grid.assert_not_awaited()


def test_switch_never_searches_grid_and_accepts_store_image(monkeypatch):
    store = AsyncMock(return_value=("https://nintendo.example/square.jpg",))
    monkeypatch.setattr(sources, "_lookup", store)
    assert asyncio.run(sources.native_cover_urls(NS(platform="switch", name="Game", image_url=None))) == ("https://nintendo.example/square.jpg",)
    sources._grid_urls.assert_not_awaited()


def test_ps_official_fallback_does_not_get_redirected_to_steam(monkeypatch):
    monkeypatch.setattr(sources, 'local_steam_app_id', lambda name: '123')
    lookup = AsyncMock(return_value=('https://ps/16x9.jpg',))
    monkeypatch.setitem(sys.modules, 'qq_bot.personal_rank_ps_store', NS(ps_store_cover_urls=lookup))
    assert asyncio.run(sources.native_cover_urls(NS(platform='ps',name='Game',image_url=None), store_only=True)) == ('https://ps/16x9.jpg',)
    lookup.assert_awaited_once()
    sources._grid_urls.assert_not_awaited()


def test_ps_public_store_error_falls_back_to_grid(monkeypatch):
    monkeypatch.setattr(sources, '_lookup', AsyncMock(side_effect=TimeoutError))
    monkeypatch.setattr(sources, '_grid_urls', AsyncMock(return_value=('https://grid/art.jpg',)))
    assert asyncio.run(sources.native_cover_urls(NS(platform='ps',name='Game',image_url=None))) == ('https://grid/art.jpg',)


def test_ps_grid_only_does_not_repeat_failed_official_download(monkeypatch):
    official = AsyncMock(side_effect=AssertionError('official must not repeat'))
    monkeypatch.setattr(sources, '_lookup', official)
    monkeypatch.setattr(sources, '_grid_urls', AsyncMock(return_value=('https://grid/art.jpg',)))
    assert asyncio.run(sources.native_cover_urls(NS(platform='ps',name='Game',image_url=None),grid_only=True)) == ('https://grid/art.jpg',)

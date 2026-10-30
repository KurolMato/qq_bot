import asyncio
import hashlib
import sqlite3
import time
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock

from PIL import Image
import pytest

from qq_bot import personal_rank_assets as rank_assets
from qq_bot.game_time_tracker import TrackedGame


@dataclass(frozen=True)
class RankData:
    avatar_url: str | None
    games: tuple[TrackedGame, ...]
    recent_games: tuple[TrackedGame, ...]
    total_seconds: float = 9000


def _image(size: tuple[int, int], colour: str = "#45789a") -> bytes:
    image = Image.new("RGB", size, colour)
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _transparent_image(size: tuple[int, int] = (300, 170)) -> bytes:
    output = BytesIO()
    Image.new("RGBA", size, (0, 0, 0, 0)).save(output, format="PNG")
    return output.getvalue()


@pytest.fixture(autouse=True)
def isolated_image_caches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(rank_assets, "native_cover_urls", AsyncMock(return_value=()))
    monkeypatch.setattr(rank_assets, "GAME_COVER_CACHE_DIR", tmp_path / "r")
    monkeypatch.setattr(rank_assets, "GAME_CALENDAR_DB_PATH", tmp_path / "g.db")
    monkeypatch.setattr(rank_assets, "GAME_CALENDAR_IMAGE_DIR", tmp_path / "i")
    url_cache = tmp_path / "u"
    monkeypatch.setattr(
        rank_assets,
        "_url_cache_path",
        lambda url: url_cache / f"{hashlib.sha256(url.encode()).hexdigest()}.img",
    )


def _catalog_db(path: Path, rows):
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE game_releases "
            "(app_id TEXT, name TEXT, image_url TEXT, platform TEXT, updated_at TEXT)"
        )
        connection.executemany("INSERT INTO game_releases VALUES (?, ?, ?, ?, ?)", rows)
        connection.commit()
    finally:
        connection.close()


def test_traditional_title_reuses_simplified_cover_offline(monkeypatch):
    simple = TrackedGame("ps", "空之轨迹 the 2nd", 900, "same-url")
    traditional = TrackedGame("ps", "空之軌跡 the 2nd", 900, "same-url")
    path = rank_assets._cover_cache_path(simple)
    path.parent.mkdir(parents=True)
    cover = _image((640, 360))
    path.write_bytes(cover)
    assert rank_assets._cover_cache_path(traditional) == path
    monkeypatch.setattr(rank_assets, "_load_image", AsyncMock(side_effect=AssertionError("must reuse cover")))
    data = RankData(None, (traditional,), (traditional,))
    result, assets = asyncio.run(rank_assets.load_personal_rank_assets(data))
    assert assets[result.games[0].image_url] == cover
    assert result.games[0].name == "空之軌跡 the 2nd"
    assert result.recent_games[0].image_url == result.games[0].image_url
    rank_assets.native_cover_urls.assert_not_awaited()
    for platform, title in (("ps", "空之轨迹 the 1st"), ("switch", simple.name),
                            ("ps", "空之轨迹 the 2nd 体验版")):
        assert rank_assets._cover_cache_path(TrackedGame(platform, title, 1, None)) != path


def test_legacy_traditional_cache_is_preserved_and_promoted(monkeypatch):
    game = TrackedGame("ps", "空之軌跡 the 2nd", 900, None)
    legacy = rank_assets._legacy_cover_cache_path(game)
    legacy.parent.mkdir(parents=True)
    cover = _image((640, 360))
    legacy.write_bytes(cover)
    canonical = rank_assets._cover_cache_path(game)
    assert legacy != canonical
    monkeypatch.setattr(rank_assets, "_load_image", AsyncMock(side_effect=AssertionError("must reuse legacy")))
    result, assets = asyncio.run(rank_assets.load_personal_rank_assets(RankData(None, (game,), ())))
    assert assets[result.games[0].image_url] == cover
    assert legacy.read_bytes() == canonical.read_bytes() == cover
    rank_assets.native_cover_urls.assert_not_awaited()


def test_traditional_title_matches_simplified_calendar():
    _catalog_db(rank_assets.GAME_CALENDAR_DB_PATH, [
        ("4225980", "空之轨迹 the 2nd", "https://example.test/cover", "Steam", "2026-10-29"),
        ("3447040", "空之轨迹 the 1st", "https://example.test/wrong", "Steam", "2026-10-29"),
    ])
    game = TrackedGame("ps", "空之軌跡 the 2nd", 900, None)
    assert rank_assets._read_catalog_matches((game,)) == {
        rank_assets._cover_identity(game): ("https://example.test/cover",),
    }


def test_portrait_and_broken_images_are_not_used(monkeypatch):
    landscape = TrackedGame("steam", "Good", 3600, "good")
    portrait = TrackedGame("ps", "Portrait", 1800, "portrait")
    broken = TrackedGame("steam", "Broken", 900, "broken")
    data = RankData("avatar", (landscape, portrait, broken), (portrait,))
    contents = {"good": _image((300, 170)), "portrait": _image((150, 240)), "broken": b"not an image"}

    async def load(url, _semaphore):
        if url == "avatar":
            raise OSError("offline")
        return contents[url]

    monkeypatch.setattr(rank_assets, "_load_image", load)
    result, assets = asyncio.run(rank_assets.load_personal_rank_assets(data))

    assert result.total_seconds == data.total_seconds
    assert result.games[0].image_url == rank_assets._cover_cache_key(landscape)
    assert result.games[1:] == (portrait, broken)
    assert result.recent_games == data.recent_games
    assert set(assets) == {rank_assets._cover_cache_key(landscape)}
    assert assets[rank_assets._cover_cache_key(landscape)] == contents["good"]


def test_switch_square_cache_is_accepted_without_landscape_search(monkeypatch):
    game = TrackedGame("switch", "異度神劍 終極版 Nintendo Switch 2 Edition", 900, "switch-icon")
    square = _image((1024, 1024))
    path = rank_assets._url_cache_path(game.image_url)
    path.parent.mkdir(parents=True)
    path.write_bytes(square)
    monkeypatch.setattr(rank_assets, "_load_image", AsyncMock(side_effect=AssertionError("must use local image")))
    result, assets = asyncio.run(rank_assets.load_personal_rank_assets(RankData(None, (game,), (game,))))
    assert assets[result.games[0].image_url] == square
    assert rank_assets._cover_cache_path(game).read_bytes() == square
    rank_assets.native_cover_urls.assert_not_awaited()
    again, same_assets = asyncio.run(rank_assets.load_personal_rank_assets(RankData(None, (game,), (game,))))
    assert same_assets == assets
    assert again == result


def test_ps_download_failure_tries_grid_after_official_urls(monkeypatch):
    game = TrackedGame('ps', 'Game', 900, None)
    cover = _image((640, 360))
    async def discover(game, *, grid_only=False):
        return ('grid',) if grid_only else ('broken-official',)
    async def download(url, semaphore):
        return cover if url == 'grid' else None
    lookup = AsyncMock(side_effect=discover)
    monkeypatch.setattr(rank_assets, 'native_cover_urls', lookup)
    monkeypatch.setattr(rank_assets, '_load_image', download)
    result, assets = asyncio.run(rank_assets.load_personal_rank_assets(RankData(None, (game,), ())))
    assert assets[result.games[0].image_url] == cover
    assert lookup.await_count == 2
    assert lookup.await_args.kwargs == {'grid_only': True}


@pytest.mark.parametrize("size", [(600, 900), (1024, 1024)])
def test_store_native_art_replaces_portrait_and_ignores_old_fitted_cache(size, monkeypatch):
    game = TrackedGame("ps", "Cached original", 900, "https://example.test/cover")
    path = rank_assets._url_cache_path(game.image_url)
    path.parent.mkdir(parents=True)
    path.write_bytes(_image(size))

    async def no_network(*args):
        pytest.fail("Existing cover bytes should not need downloading")

    old = rank_assets.GAME_COVER_CACHE_DIR / rank_assets._cover_cache_path(game).name
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_bytes(_image((960, 540), "red"))
    native = _image((1280, 720), "blue")
    monkeypatch.setattr(rank_assets, "native_cover_urls", AsyncMock(return_value=("native",)))
    async def cached_source(url, semaphore):
        return native if url == "native" else path.read_bytes()

    monkeypatch.setattr(rank_assets, "_load_image", cached_source)
    data = RankData(None, (game,), ())
    first, first_assets = asyncio.run(rank_assets.load_personal_rank_assets(data))
    monkeypatch.setattr(rank_assets, "_load_image", no_network)
    second, second_assets = asyncio.run(rank_assets.load_personal_rank_assets(data))
    assert first == second
    assert first_assets == second_assets
    assert second_assets[second.games[0].image_url] == native
    rank_assets.native_cover_urls.assert_awaited_once()


def test_steam_portrait_url_uses_landscape_header_and_updates_frozen_data(monkeypatch):
    source = (
        "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/123/"
        "content-hash/library_600x900_2x.jpg?t=1234"
    )
    hashed_header = (
        "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/123/"
        "content-hash/header.jpg"
    )
    data = RankData(None, (TrackedGame("steam", "Game", 5400, source),), ())
    calls: list[str] = []
    cover = _image((460, 215))

    async def load(url, _semaphore):
        calls.append(url)
        if url == hashed_header:
            return cover
        raise OSError("header absent in conventional location")

    monkeypatch.setattr(rank_assets, "_load_image", load)
    result, assets = asyncio.run(rank_assets.load_personal_rank_assets(data))

    assert calls == [hashed_header]
    key = rank_assets._cover_cache_key(data.games[0])
    assert result.games[0].image_url == key
    assert result.games[0].seconds == 5400
    assert assets == {key: cover}


def test_ps_catalog_cover_is_read_only_fallback_for_portrait_source(tmp_path, monkeypatch):
    database = tmp_path / "game-calendar.db"
    _catalog_db(database, [
        ("psn:1", "Astro Bot", "https://catalog.example/astro.jpg", "PS", "2026-01-01"),
    ])
    monkeypatch.setattr(rank_assets, "GAME_CALENDAR_DB_PATH", database)
    portrait = _image((100, 180))
    cover = _image((500, 240))
    data = RankData(
        None,
        (TrackedGame("ps", "Astro Bot", 3600, "https://profile.example/portrait.jpg"),),
        (),
    )
    calls: list[str] = []

    async def load(url, _semaphore):
        calls.append(url)
        return portrait if "profile.example" in url else cover

    monkeypatch.setattr(rank_assets, "_load_image", load)
    result, assets = asyncio.run(rank_assets.load_personal_rank_assets(data))

    assert calls == ["https://profile.example/portrait.jpg", "https://catalog.example/astro.jpg"]
    key = rank_assets._cover_cache_key(data.games[0])
    assert result.games[0].image_url == key
    assert assets == {key: cover}
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("SELECT COUNT(*) FROM game_releases").fetchone()[0] == 1
    finally:
        connection.close()


def test_catalog_url_cache_is_used_before_any_remote_load(tmp_path, monkeypatch):
    database = tmp_path / "game-calendar.db"
    catalog_url = "https://catalog.example/astro.jpg"
    _catalog_db(database, [
        ("psn:1", "Astro Bot", catalog_url, "PS", "2026-01-01"),
    ])
    monkeypatch.setattr(rank_assets, "GAME_CALENDAR_DB_PATH", database)
    cached_cover = _image((500, 240))
    url_cache = rank_assets._url_cache_path(catalog_url)
    url_cache.parent.mkdir(parents=True, exist_ok=True)
    url_cache.write_bytes(cached_cover)
    data = RankData(None, (TrackedGame("ps", "Astro Bot", 3600, None),), ())

    async def fail_if_called(_url, _semaphore):
        raise AssertionError("a valid URL cache hit should avoid network access")

    monkeypatch.setattr(rank_assets, "_load_image", fail_if_called)
    result, assets = asyncio.run(rank_assets.load_personal_rank_assets(data))

    key = rank_assets._cover_cache_key(data.games[0])
    assert result.games[0].image_url == key
    assert assets == {key: cached_cover}
    assert rank_assets._cover_cache_path(data.games[0]).read_bytes() == cached_cover


def test_concurrent_image_loads_are_bounded_and_unshown_games_are_not_loaded(monkeypatch):
    monkeypatch.setattr(rank_assets, "MAX_CONCURRENT_IMAGE_LOADS", 2)
    games = tuple(TrackedGame("steam", f"Game {index}", 900, f"url-{index}") for index in range(7))
    recent = (
        TrackedGame("ps", "Recent Low", 120, "recent-low"),
        TrackedGame("ps", "Recent Hero", 360, "recent-hero"),
        TrackedGame("ps", "Recent Mid", 240, "recent-mid"),
    )
    data = RankData(None, games, recent)
    active = 0
    peak = 0

    async def load(_url, _semaphore):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.01)
            return _image((300, 170))
        finally:
            active -= 1

    monkeypatch.setattr(rank_assets, "_load_image", load)
    result, assets = asyncio.run(rank_assets.load_personal_rank_assets(data))

    assert len(result.games) == 7
    assert len(assets) == 7
    assert rank_assets._cover_cache_key(games[6]) not in assets
    assert rank_assets._cover_cache_key(recent[1]) in assets
    assert not any(rank_assets._cover_cache_key(game) in assets for game in (recent[0], recent[2]))
    assert peak <= 2


def test_per_game_cache_survives_source_url_change_without_network(monkeypatch):
    first = TrackedGame("switch", "Example: Ultimate Edition 2", 3600, "https://a.example/cover")
    cover = _image((440, 230))
    calls: list[str] = []

    async def load(url, _semaphore):
        calls.append(url)
        return cover

    monkeypatch.setattr(rank_assets, "_load_image", load)
    first_result, first_assets = asyncio.run(
        rank_assets.load_personal_rank_assets(RankData(None, (first,), ()))
    )
    assert first_result.games[0].image_url == rank_assets._cover_cache_key(first)
    assert first_assets == {rank_assets._cover_cache_key(first): cover}
    assert rank_assets._cover_cache_path(first).read_bytes() == cover

    second = TrackedGame(
        "switch", "Example: Ultimate Edition 2", 7200, "https://b.example/changed-cover"
    )

    async def fail_offline(_url, _semaphore):
        raise AssertionError("the per-game cache should make this call offline")

    monkeypatch.setattr(rank_assets, "_load_image", fail_offline)
    second_result, second_assets = asyncio.run(
        rank_assets.load_personal_rank_assets(RankData(None, (second,), ()))
    )

    assert calls == ["https://a.example/cover"]
    assert second_result.games[0].image_url == rank_assets._cover_cache_key(second)
    assert second_assets == {rank_assets._cover_cache_key(second): cover}
    assert rank_assets._cover_cache_path(second) == rank_assets._cover_cache_path(first)
    assert rank_assets._cover_cache_path(
        TrackedGame("switch", "Example: Ultimate Edition 3", 1, None)
    ) != rank_assets._cover_cache_path(first)


@pytest.mark.parametrize(
    "seed",
    [b"corrupt cache", _image((120, 240)), _transparent_image()],
    ids=["corrupt", "portrait", "transparent"],
)
def test_invalid_per_game_cache_is_repaired_from_source(seed, monkeypatch):
    game = TrackedGame("steam", "Repair Cache Game", 3600, "https://source.example/game.jpg")
    path = rank_assets._cover_cache_path(game)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(seed)
    cover = _image((440, 230))
    calls: list[str] = []

    async def load(url, _semaphore):
        calls.append(url)
        return cover

    monkeypatch.setattr(rank_assets, "_load_image", load)
    result, assets = asyncio.run(
        rank_assets.load_personal_rank_assets(RankData(None, (game,), ()))
    )

    key = rank_assets._cover_cache_key(game)
    assert calls == ["https://source.example/game.jpg"]
    assert result.games[0].image_url == key
    assert assets == {key: cover}
    assert path.read_bytes() == cover
    assert rank_assets._valid_asset(path.read_bytes(), landscape=True)


def test_batch_deadline_returns_when_images_stall(monkeypatch):
    monkeypatch.setattr(rank_assets, "IMAGE_BATCH_DEADLINE_SECONDS", 0.04)
    data = RankData(
        None,
        tuple(TrackedGame("steam", f"Game {index}", 900, f"slow-{index}") for index in range(4)),
        (),
    )

    async def load(_url, _semaphore):
        await asyncio.sleep(5)
        return _image((300, 170))

    monkeypatch.setattr(rank_assets, "_load_image", load)
    started = time.monotonic()
    result, assets = asyncio.run(rank_assets.load_personal_rank_assets(data))

    assert time.monotonic() - started < 0.5
    assert len(result.games) == 4
    assert assets == {}


def test_cancelled_image_does_not_abort_other_assets(monkeypatch):
    data = RankData(
        None,
        (
            TrackedGame("steam", "Canceled", 900, "cancel"),
            TrackedGame("steam", "Available", 900, "available"),
        ),
        (),
    )
    cover = _image((300, 170))

    async def load(url, _semaphore):
        if url == "cancel":
            raise asyncio.CancelledError
        return cover

    monkeypatch.setattr(rank_assets, "_load_image", load)
    _, assets = asyncio.run(rank_assets.load_personal_rank_assets(data))

    assert assets == {rank_assets._cover_cache_key(data.games[1]): cover}


def test_cancelling_rank_load_cancels_owned_image_tasks(monkeypatch):
    async def scenario():
        started = asyncio.Event()
        cleaned_up = asyncio.Event()

        async def load(_url, _semaphore):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cleaned_up.set()
                raise

        monkeypatch.setattr(rank_assets, "_load_image", load)
        data = RankData(None, (TrackedGame("steam", "Game", 3600, "slow"),), ())
        task = asyncio.create_task(rank_assets.load_personal_rank_assets(data))
        await started.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return cleaned_up.is_set()

    assert asyncio.run(scenario())


def test_steam_header_conversion_handles_hashed_and_rejects_non_steam_urls():
    assert rank_assets._steam_header_urls(
        "https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/42/hash/"
        "library_600x900_2x.jpg?revision=4"
    ) == (
        "https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/42/hash/header.jpg",
        "https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/42/header.jpg",
    )
    assert rank_assets._steam_header_urls(
        "https://images.example/steam/apps/42/library_600x900_2x.jpg"
    ) == ()


def test_calendar_fallback_requires_exact_normalized_title(tmp_path, monkeypatch):
    database = tmp_path / "g.db"
    _catalog_db(database, [
        ("psn:1", "Nioh 2", "https://catalog.example/nioh2.jpg", "PS", "2026-01-01"),
    ])
    monkeypatch.setattr(rank_assets, "GAME_CALENDAR_DB_PATH", database)
    games = (TrackedGame("ps", "Nioh 3", 3600, None),)
    data = RankData(None, games, ())
    calls: list[str] = []

    async def load(url, _semaphore):
        calls.append(url)
        return _image((500, 240))

    monkeypatch.setattr(rank_assets, "_load_image", load)
    result, assets = asyncio.run(rank_assets.load_personal_rank_assets(data))

    assert result.games == games
    assert assets == {}
    assert calls == []


def test_steam_uses_conventional_header_after_hashed_header_fails(monkeypatch):
    source = (
        "https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/42/hash/"
        "library_600x900_2x.jpg"
    )
    hashed = "https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/42/hash/header.jpg"
    conventional = "https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/42/header.jpg"
    cover = _image((460, 215))
    data = RankData(None, (TrackedGame("steam", "Game", 3600, source),), ())
    calls: list[str] = []

    async def load(url, _semaphore):
        calls.append(url)
        if url == conventional:
            return cover
        raise OSError("hashed header absent")

    monkeypatch.setattr(rank_assets, "_load_image", load)
    result, assets = asyncio.run(rank_assets.load_personal_rank_assets(data))

    assert calls == [hashed, conventional]
    key = rank_assets._cover_cache_key(data.games[0])
    assert result.games[0].image_url == key
    assert assets == {key: cover}

import asyncio
import sqlite3
from pathlib import Path

import httpx
import pytest

import qq_bot.steam_registry as steam_module
from qq_bot.steam_registry import (
    SteamRegistry,
    SteamClient,
    SteamSubscription,
    _should_notify,
    normalize_steam_identifier,
    parse_steam_activity,
    steam_user_message,
)


def subscription(**overrides: object) -> SteamSubscription:
    values: dict[str, object] = {
        "steam_id": "76561198000000000",
        "display_name": "Steam Player",
        "identifier": "steam_player",
        "avatar_url": "https://example.com/avatar.jpg",
        "qq_user_id": "100",
        "group_id": "200",
        "status": "active",
        "current_game": None,
        "initialised": False,
        "force_notify": False,
    }
    values.update(overrides)
    return SteamSubscription(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(("value", "expected"), [
    ("76561198000000000", "76561198000000000"),
    ("steam_player", "steam_player"),
    ("39734272", "76561198000000000"),
    (" 39734272 ", "76561198000000000"),
    ("4294967295", "76561202255233023"),
    ("https://steamcommunity.com/id/steam_player/", "steam_player"),
    ("https://steamcommunity.com/profiles/76561198000000000/", "76561198000000000"),
])
def test_normalize_steam_identifier(value: str, expected: str) -> None:
    assert normalize_steam_identifier(value) == expected


@pytest.mark.parametrize("value", ["", "x", "https://example.com/id/player", "bad value", "0", "4294967296", "123456789012"])
def test_reject_invalid_steam_identifier(value: str) -> None:
    with pytest.raises(ValueError):
        normalize_steam_identifier(value)


def test_friend_code_lookup_skips_vanity_api(monkeypatch: pytest.MonkeyPatch) -> None:
    client = SteamClient()

    async def get(path: str, **params: str) -> dict:
        assert path == "ISteamUser/GetPlayerSummaries/v2/"
        assert params["steamids"] == "76561198000000000"
        return {"response": {"players": [{"steamid": params["steamids"], "communityvisibilitystate": 3}]}}

    monkeypatch.setattr(client, "_get", get)
    assert asyncio.run(client.lookup("39734272"))["steamid"] == "76561198000000000"


def test_numeric_vanity_link_is_not_friend_code(monkeypatch: pytest.MonkeyPatch) -> None:
    client = SteamClient()

    async def get(path: str, **params: str) -> dict:
        assert path == "ISteamUser/ResolveVanityURL/v1/"
        assert params["vanityurl"] == "39734272"
        return {"response": {"success": 1, "steamid": "76561198000000001"}}

    monkeypatch.setattr(client, "_get", get)
    assert asyncio.run(client.resolve("https://steamcommunity.com/id/39734272/")) == "76561198000000001"


def test_registry_supports_multiple_groups_and_group_member_remove(tmp_path: Path) -> None:
    registry = SteamRegistry(tmp_path / "steam.db")
    first = subscription()
    second = subscription(group_id="201", qq_user_id="101")
    registry.add(first)
    registry.add(second)
    assert registry.list_group("200") == [first]
    assert registry.list_group("201") == [second]
    assert registry.remove("steam_player", "200", "other") is True
    assert registry.list_group("201") == [second]


def test_parse_steam_activity() -> None:
    activity = parse_steam_activity(
        subscription(),
        {
            "steamid": "76561198000000000",
            "personaname": "New Name",
            "personastate": 1,
            "avatarfull": "https://example.com/new.jpg",
            "gameid": "570",
            "gameextrainfo": "Dota 2",
        },
    )
    assert activity.account_name == "New Name"
    assert activity.game_name == "Dota 2"
    assert activity.game_key == "dota 2"
    assert activity.platform == "Steam"
    assert "/570/library_600x900_2x.jpg" in str(activity.game_image_url)


def test_steam_notification_only_on_start_or_change() -> None:
    assert _should_notify(subscription(), "dota 2") is True
    assert _should_notify(subscription(initialised=True, current_game="Dota 2"), "dota 2") is False
    assert _should_notify(subscription(initialised=True, current_game="Dota 2"), "portal 2") is True
    assert _should_notify(subscription(initialised=True), None) is False
    assert _should_notify(
        subscription(initialised=True, current_game="赛博朋克 2077"),
        "cyberpunk 2077",
        "赛博朋克 2077",
    ) is False


def test_game_name_uses_official_simplified_chinese_and_caches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = SteamClient()
    calls = 0

    async def appdetails(_url: str, **params: str) -> dict[str, object]:
        nonlocal calls
        calls += 1
        assert params["l"] == "schinese"
        assert params["cc"] == "cn"
        return {
            "1091500": {
                "success": True,
                "data": {"name": "赛博朋克 2077", "header_image": "https://example.com/header.jpg"},
            }
        }

    monkeypatch.setattr(client, "_public_get", appdetails)

    assert asyncio.run(client.game_name("1091500")) == "赛博朋克 2077"
    assert asyncio.run(client.game_name("1091500")) == "赛博朋克 2077"
    assert calls == 1


def test_runtime_metadata_cache_has_a_size_limit() -> None:
    client = SteamClient()
    client._cache_limit = 32

    for index in range(40):
        client._remember(client._metadata_cache, str(index), {"name": str(index)})

    assert len(client._metadata_cache) == 32
    assert "0" not in client._metadata_cache


def test_steam_api_retries_read_error_and_enables_proxy_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = SteamClient()
    calls = 0
    trust_modes: list[bool] = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"response": {"players": []}}

    class FakeAsyncClient:
        def __init__(self, **kwargs: object) -> None:
            trust_modes.append(bool(kwargs["trust_env"]))

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def get(self, _url: str, **_kwargs: object) -> FakeResponse:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise httpx.ReadError(
                    "connection interrupted",
                    request=httpx.Request("GET", "https://api.steampowered.com"),
                )
            return FakeResponse()

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(client, "_key", lambda: "a" * 32)
    monkeypatch.setattr(steam_module.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(steam_module.asyncio, "sleep", no_sleep)

    payload = asyncio.run(client._get("ISteamUser/GetPlayerSummaries/v2/"))

    assert payload == {"response": {"players": []}}
    assert calls == 2
    assert trust_modes == [False, True]


def test_game_name_persists_across_client_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = SteamRegistry(tmp_path / "steam.db")
    first_client = SteamClient(registry)

    async def appdetails(_url: str, **_params: str) -> dict[str, object]:
        return {
            "1091500": {
                "success": True,
                "data": {"name": "赛博朋克 2077"},
            }
        }

    monkeypatch.setattr(first_client, "_public_get", appdetails)
    assert asyncio.run(first_client.game_name("1091500")) == "赛博朋克 2077"

    restarted_client = SteamClient(SteamRegistry(tmp_path / "steam.db"))

    async def should_not_fetch(_url: str, **_params: str) -> dict[str, object]:
        raise AssertionError("persistent Chinese title should be used after restart")

    monkeypatch.setattr(restarted_client, "_public_get", should_not_fetch)
    assert asyncio.run(restarted_client.game_name("1091500")) == "赛博朋克 2077"


def test_registry_discards_unverified_legacy_localized_names_once(tmp_path: Path) -> None:
    database = tmp_path / "steam.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE steam_localized_game_names (
                app_id TEXT PRIMARY KEY,
                localized_name TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO steam_localized_game_names VALUES (?, ?, ?)",
            ("123", "上一个游戏的错误中文名", "2026-08-31T00:00:00+00:00"),
        )

    registry = SteamRegistry(database)
    assert registry.localized_game_name("123") is None
    assert registry.remember_localized_game_name("456", "正确中文名") is True

    restarted = SteamRegistry(database)
    assert restarted.localized_game_name("456") == "正确中文名"


def test_only_cjk_game_names_are_persisted(tmp_path: Path) -> None:
    registry = SteamRegistry(tmp_path / "steam.db")
    assert registry.remember_localized_game_name("570", "Dota 2") is False
    assert registry.localized_game_name("570") is None
    assert registry.remember_localized_game_name("570", "刀塔 2") is True
    assert registry.localized_game_name("570") == "刀塔 2"


def test_game_cover_falls_back_to_store_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    client = SteamClient()

    async def unavailable(_url: str) -> bool:
        return False

    async def appdetails(_url: str, **_params: str) -> dict[str, object]:
        return {
            "3513350": {
                "success": True,
                "data": {"header_image": "https://example.com/hashed/header.jpg"},
            }
        }

    monkeypatch.setattr(client, "_url_available", unavailable)
    monkeypatch.setattr(client, "_steamgriddb_cover", lambda _app_id: _async_value(None))
    monkeypatch.setattr(client, "_public_get", appdetails)
    assert asyncio.run(client.game_cover("3513350")) == "https://example.com/hashed/header.jpg"
    assert asyncio.run(client.game_cover("3513350")) == "https://example.com/hashed/header.jpg"


async def _async_value(value: object) -> object:
    return value


def test_game_cover_prefers_steamgriddb_portrait_before_store_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = SteamClient()

    async def unavailable(_url: str) -> bool:
        return False

    async def portrait(_app_id: str) -> str:
        return "https://cdn.steamgriddb.com/grid/portrait.png"

    async def appdetails(_url: str, **_params: str) -> dict[str, object]:
        raise AssertionError("store header fallback should not run when a portrait exists")

    monkeypatch.setattr(client, "_url_available", unavailable)
    monkeypatch.setattr(client, "_steamgriddb_cover", portrait)
    monkeypatch.setattr(client, "_public_get", appdetails)

    assert asyncio.run(client.game_cover("3764200")) == (
        "https://cdn.steamgriddb.com/grid/portrait.png"
    )
    assert asyncio.run(client.game_cover("3764200")) == (
        "https://cdn.steamgriddb.com/grid/portrait.png"
    )


@pytest.mark.parametrize(("error", "expected"), [
    (RuntimeError("401 key invalid"), "API Key 无效"),
    (RuntimeError("429 rate limit"), "查询过于频繁"),
    (RuntimeError("C:/private stack"), "暂时无法完成操作"),
])
def test_steam_errors_are_safe_for_group(error: BaseException, expected: str) -> None:
    message = steam_user_message(error)
    assert expected in message
    assert "C:/" not in message
    assert "stack" not in message

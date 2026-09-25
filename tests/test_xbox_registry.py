import asyncio
import importlib
from pathlib import Path

import pytest

import qq_bot.xbox_registry as xbox_registry
from qq_bot.xbox_registry import (
    XboxError,
    XboxClient,
    XboxRegistry,
    XboxSubscription,
    XboxVisibilityError,
    _presence_records,
    _people_presence_records,
    _should_notify,
    normalize_gamertag,
    parse_xbox_activity,
    parse_xbox_profile,
    xbox_user_message,
)


def test_direct_call_persists_refreshed_token_before_business_callback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_path = tmp_path / "xbox-tokens.json"
    token_path.write_text("old-token", encoding="utf-8")
    monkeypatch.setattr(xbox_registry, "_token_path", lambda: token_path)

    class FakeOAuth:
        def __init__(self, value: str) -> None:
            self.value = value

        @classmethod
        def model_validate_json(cls, value: str) -> "FakeOAuth":
            return cls(value)

        def model_dump_json(self) -> str:
            return self.value

    class FakeManager:
        def __init__(self, *_args: object) -> None:
            self.oauth: FakeOAuth | None = None

        async def refresh_tokens(self) -> None:
            self.oauth = FakeOAuth("new-token")

    class FakeSession:
        async def __aenter__(self) -> "FakeSession":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    class FakeLiveClient:
        def __init__(self, _manager: FakeManager) -> None:
            return None

    manager_module = importlib.import_module("xbox.webapi.authentication.manager")
    model_module = importlib.import_module("xbox.webapi.authentication.models")
    client_module = importlib.import_module("xbox.webapi.api.client")
    session_module = importlib.import_module("xbox.webapi.common.signed_session")
    monkeypatch.setattr(manager_module, "AuthenticationManager", FakeManager)
    monkeypatch.setattr(model_module, "OAuth2TokenResponse", FakeOAuth)
    monkeypatch.setattr(client_module, "XboxLiveClient", FakeLiveClient)
    monkeypatch.setattr(session_module, "SignedSession", FakeSession)

    events: list[str] = []

    def save(path: Path, value: str) -> None:
        events.append(f"save:{value}")
        path.write_text(value, encoding="utf-8")

    monkeypatch.setattr(xbox_registry, "atomic_write_text", save)

    async def callback(_client: object) -> None:
        events.append("callback")
        assert token_path.read_text(encoding="utf-8") == "new-token"
        raise RuntimeError("temporary API failure")

    with pytest.raises(XboxError):
        asyncio.run(XboxClient()._direct_call("probe", callback))

    assert events == ["save:new-token", "callback"]
    assert token_path.read_text(encoding="utf-8") == "new-token"


def subscription(**overrides: object) -> XboxSubscription:
    values: dict[str, object] = {
        "gamertag": "Major Nelson",
        "xuid": "2533274843156789",
        "avatar_url": "https://example.com/avatar.png",
        "qq_user_id": "100",
        "group_id": "200",
    }
    values.update(overrides)
    return XboxSubscription(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", ["示例玩家", "Major Nelson", "Player#1234"])
def test_normalize_gamertag(value: str) -> None:
    assert normalize_gamertag(value) == value


@pytest.mark.parametrize("value", ["", "A" * 17, "bad\nname"])
def test_reject_invalid_gamertag(value: str) -> None:
    with pytest.raises(ValueError):
        normalize_gamertag(value)


def test_parse_compact_profile() -> None:
    profile = parse_xbox_profile(
        {
            "content": {
                "xuid": "2533274843156789",
                "gamertag": "Major Nelson",
                "profilePicture": "https://example.com/avatar.png",
            }
        },
        "Major Nelson",
    )
    assert profile == {
        "xuid": "2533274843156789",
        "gamertag": "Major Nelson",
        "avatar_url": "https://example.com/avatar.png",
    }


def test_parse_documented_search_profile() -> None:
    profile = parse_xbox_profile(
        {
            "people": [{
                "xuid": "42",
                "uniqueModernGamertag": "Player#1234",
                "displayPicRaw": "https://example.com/gamerpic.png",
            }]
        },
        "Player#1234",
    )
    assert profile["xuid"] == "42"
    assert profile["gamertag"] == "Player#1234"


def test_presence_parser_accepts_xuid_map() -> None:
    payload = {"42": {"state": "Online", "devices": []}}
    assert _presence_records(payload)["42"]["state"] == "Online"


def test_parse_xbox_activity_prefers_active_foreground_game() -> None:
    activity = parse_xbox_activity(
        subscription(),
        {
            "xuid": "2533274843156789",
            "state": "Online",
            "devices": [{
                "type": "XboxSeriesX",
                "titles": [
                    {"name": "Microsoft Store", "state": "Active", "placement": "Full"},
                    {"name": "Halo Infinite", "state": "Active", "placement": "Full", "imageUri": "https://example.com/halo.jpg"},
                ],
            }],
        },
    )
    assert activity.game_name == "Halo Infinite"
    assert activity.game_key == "halo infinite"
    assert activity.game_image_url == "https://example.com/halo.jpg"
    assert activity.platform == "Xbox"


def test_parse_xbox_activity_removes_changing_rich_presence_suffix() -> None:
    first = parse_xbox_activity(
        subscription(),
        {
            "xuid": "42",
            "state": "Online",
            "devices": [{
                "titles": [{
                    "name": "Forza Horizon 6 - Hot Lapping at the Horizon Festival",
                    "state": "Active",
                    "placement": "Full",
                    "isGame": True,
                }]
            }],
        },
    )
    second = parse_xbox_activity(
        subscription(),
        {
            "xuid": "42",
            "state": "Online",
            "devices": [{
                "titles": [{
                    "name": "Forza Horizon 6 - Driving around Japan",
                    "state": "Active",
                    "placement": "Full",
                    "isGame": True,
                }]
            }],
        },
    )

    assert first.game_name == "Forza Horizon 6"
    assert second.game_name == "Forza Horizon 6"
    assert first.game_key == second.game_key == "forza horizon 6"
    assert _should_notify(
        subscription(
            initialised=True,
            current_game="Forza Horizon 6 - Previous activity",
        ),
        second.game_key,
    ) is False


@pytest.mark.parametrize("title", ["Online", "Xbox App", "Xbox Game Bar"])
def test_parse_xbox_activity_ignores_system_titles(title: str) -> None:
    activity = parse_xbox_activity(
        subscription(),
        {"xuid": "42", "state": "Online", "devices": [{"titles": [{"name": title, "state": "Active"}]}]},
    )
    assert activity.game_name is None


def test_peoplehub_presence_uses_explicit_game_flag() -> None:
    records = _people_presence_records({
        "people": [{
            "xuid": "42",
            "presenceState": "Online",
            "titlePresence": {"titleId": "99", "titleName": "Halo Infinite"},
            "presenceDetails": [
                {"IsGame": False, "TitleId": "1", "PresenceText": "Xbox App", "State": "Active"},
                {"IsGame": True, "TitleId": "99", "PresenceText": "In menus", "State": "Active"},
            ],
        }]
    })
    activity = parse_xbox_activity(subscription(), records["42"])
    assert activity.game_name == "Halo Infinite"


def test_registry_supports_multiple_groups_and_unrestricted_remove(tmp_path: Path) -> None:
    registry = XboxRegistry(tmp_path / "xbox.db")
    first = subscription()
    second = subscription(group_id="201", qq_user_id="101")
    registry.add(first)
    registry.add(second)
    assert registry.remove("major nelson", "200", "other") is True
    assert registry.list_group("201") == [second]


def test_notification_only_on_start_or_change() -> None:
    assert _should_notify(subscription(), "halo infinite") is True
    assert _should_notify(subscription(initialised=True, current_game="Halo Infinite"), "halo infinite") is False
    assert _should_notify(subscription(initialised=True, current_game="Halo Infinite"), "forza") is True
    assert _should_notify(subscription(initialised=True), None) is False


def test_lookup_reports_hidden_presence(monkeypatch: pytest.MonkeyPatch) -> None:
    client = XboxClient()

    async def get(path: str):
        if path.startswith("player/"):
            return {"xuid": "42", "gamertag": "Player"}
        return []

    monkeypatch.setattr(XboxClient, "direct_configured", property(lambda _: False))
    monkeypatch.setattr(XboxClient, "openxbl_configured", property(lambda _: True))
    monkeypatch.setattr(client, "_openxbl_get", get)
    with pytest.raises(XboxVisibilityError, match="所有人可见"):
        asyncio.run(client.lookup("Player"))


def test_direct_provider_is_preferred(monkeypatch: pytest.MonkeyPatch) -> None:
    client = XboxClient()
    calls: list[str] = []

    async def direct_call(operation: str, callback: object) -> str:
        calls.append(operation)
        return "direct"

    async def fallback() -> str:
        return "fallback"

    monkeypatch.setattr(XboxClient, "direct_configured", property(lambda _: True))
    monkeypatch.setattr(XboxClient, "openxbl_configured", property(lambda _: True))
    monkeypatch.setattr(client, "_direct_call", direct_call)
    result = asyncio.run(client._with_fallback("status", object(), fallback))
    assert result == "direct"
    assert calls == ["status"]


def test_direct_lookup_requests_full_presence(monkeypatch: pytest.MonkeyPatch) -> None:
    client = XboxClient()
    requested_level: list[str] = []

    class Profile:
        async def get_profile_by_gamertag(self, gamertag: str):
            return {
                "profileUsers": [{
                    "id": "42",
                    "settings": [
                        {"id": "Gamertag", "value": gamertag},
                        {"id": "GameDisplayPicRaw", "value": "https://example.com/avatar.png"},
                    ],
                }]
            }

    class Presence:
        async def get_presence_batch(self, xuids: list[str], presence_level: str):
            requested_level.append(presence_level)
            return [{"xuid": xuids[0], "state": "Online", "devices": []}]

    class FakeClient:
        profile = Profile()
        presence = Presence()

    async def direct_call(operation: str, callback: object):
        return await callback(FakeClient())  # type: ignore[operator]

    monkeypatch.setattr(XboxClient, "direct_configured", property(lambda _: True))
    monkeypatch.setattr(XboxClient, "openxbl_configured", property(lambda _: False))
    monkeypatch.setattr(client, "_direct_call", direct_call)
    profile = asyncio.run(client.lookup("ExampleXboxPlayer"))
    assert profile["xuid"] == "42"
    assert requested_level == ["all"]


@pytest.mark.parametrize(("error", "expected"), [
    (RuntimeError("404 Not Found"), "没有找到"),
    (RuntimeError("401 Unauthorized"), "API Key 已失效"),
    (RuntimeError("429 rate limit"), "额度暂时用完"),
    (RuntimeError("C:/private/stack"), "暂时无法完成操作"),
])
def test_errors_are_safe_for_group(error: BaseException, expected: str) -> None:
    message = xbox_user_message(error)
    assert expected in message
    assert "C:/" not in message

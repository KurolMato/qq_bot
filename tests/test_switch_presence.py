import json
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from qq_bot.switch_presence import (
    _download_image,
    _nxapi_has_subscriptions,
    _render_activity_card,
    _resize_image,
    load_switch_config,
    parse_switch_activity,
)


@pytest.mark.asyncio
async def test_legacy_switch_monitor_detects_nxapi_subscriptions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeRegistry:
        @staticmethod
        def list_all() -> list[object]:
            return [object()]

    monkeypatch.setattr("qq_bot.switch_registry.registry", FakeRegistry())

    assert await _nxapi_has_subscriptions() is True


@pytest.mark.asyncio
async def test_legacy_switch_monitor_remains_available_without_nxapi_subscriptions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeRegistry:
        @staticmethod
        def list_all() -> list[object]:
            return []

    monkeypatch.setattr("qq_bot.switch_registry.registry", FakeRegistry())

    assert await _nxapi_has_subscriptions() is False


@pytest.mark.asyncio
async def test_download_image_accepts_valid_jpeg_with_octet_stream_mime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = BytesIO()
    Image.new("RGB", (80, 45), "blue").save(source, format="JPEG")

    class Response:
        content = source.getvalue()
        headers = {"content-type": "application/octet-stream"}

        @staticmethod
        def raise_for_status() -> None:
            return None

    class Client:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def get(self, _url: str) -> Response:
            return Response()

    monkeypatch.setattr("qq_bot.switch_presence.httpx.AsyncClient", Client)

    content = await _download_image("https://img-eshop.cdn.nintendo.net/i/game.jpg")

    assert content == source.getvalue()


def test_parse_nxapi_friend_payload() -> None:
    activity = parse_switch_activity({
        "friend": {
            "name": "NS昵称",
            "imageUri": "https://example.com/avatar.png",
            "presence": {
                "state": "ONLINE",
                "game": {
                    "name": "The Legend of Zelda",
                    "imageUri": "https://example.com/game.jpg",
                },
            },
        }
    })
    assert activity.account_name == "NS昵称"
    assert activity.game_name == "The Legend of Zelda"
    assert activity.avatar_url == "https://example.com/avatar.png"
    assert activity.game_image_url == "https://example.com/game.jpg"
    assert activity.platform == "Switch"
    assert activity.game_key == "the legend of zelda"


def test_offline_has_no_game_key() -> None:
    activity = parse_switch_activity({"name": "A", "presence": {"state": "OFFLINE", "game": None}})
    assert activity.game_key is None


def test_resize_switch_image_limits_dimensions() -> None:
    source = BytesIO()
    Image.new("RGB", (1200, 800), "red").save(source, format="JPEG")

    resized = _resize_image(source.getvalue(), 240, 160)

    with Image.open(BytesIO(resized)) as image:
        assert image.size == (240, 160)


def test_render_activity_card_is_compact_png() -> None:
    avatar = BytesIO()
    cover = BytesIO()
    Image.new("RGB", (256, 256), "purple").save(avatar, format="PNG")
    Image.new("RGB", (800, 1200), "navy").save(cover, format="JPEG")

    card = _render_activity_card(
        "凤凰院凶真",
        "Resident Evil Requiem",
        avatar.getvalue(),
        cover.getvalue(),
        "Switch",
    )

    with Image.open(BytesIO(card)) as image:
        assert image.format == "PNG"
        assert image.size == (640, 220)


def test_load_config_inherits_default_groups(tmp_path: Path) -> None:
    path = tmp_path / "switch_presence.json"
    path.write_text(json.dumps({
        "enabled": True,
        "poll_interval_seconds": 10,
        "notify_groups": ["123"],
        "people": [{"name": "小明", "presence_url": "https://example.com/presence"}],
    }), encoding="utf-8")
    config = load_switch_config(path)
    assert config.enabled is True
    assert config.poll_interval_seconds == 30
    assert config.people[0].notify_groups == ("123",)


def test_config_rejects_insecure_url(tmp_path: Path) -> None:
    path = tmp_path / "switch_presence.json"
    path.write_text(json.dumps({
        "enabled": True,
        "notify_groups": ["123"],
        "people": [{"name": "小明", "presence_url": "http://example.com"}],
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="HTTPS"):
        load_switch_config(path)


def test_config_allows_local_nxapi_http_server(tmp_path: Path) -> None:
    path = tmp_path / "switch_presence.json"
    path.write_text(json.dumps({
        "enabled": True,
        "notify_groups": ["123"],
        "people": [{"name": "小明", "presence_url": "http://127.0.0.1:12345/api/znc/friend/abc/presence"}],
    }), encoding="utf-8")
    assert load_switch_config(path).people[0].name == "小明"

import asyncio
from io import BytesIO
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import httpx
import pytest
from PIL import Image

from qq_bot import switch_presence as presence


AVATAR_PATH = "/88ca271e657a0882145b8764db8c4fdb7ea4c128_full.jpg"
ORIGINAL = "https://avatars.steamstatic.com" + AVATAR_PATH
AKAMAI = "https://avatars.akamai.steamstatic.com" + AVATAR_PATH
FASTLY = "https://avatars.fastly.steamstatic.com" + AVATAR_PATH


def avatar_bytes():
    output = BytesIO()
    Image.new("RGB", (16, 16), "blue").save(output, "PNG")
    return output.getvalue()


@pytest.mark.parametrize("host", presence.STEAM_AVATAR_HOSTS)
def test_original_host_first_and_same_avatar_path_preserved(host):
    url = f"https://{host}{AVATAR_PATH}?size=184#avatar"
    candidates = presence._steam_avatar_urls(url)
    assert candidates[0] == url
    assert len(set(candidates)) == 3
    assert {urlsplit(candidate).hostname for candidate in candidates} == set(presence.STEAM_AVATAR_HOSTS)
    assert all(urlsplit(candidate).path == AVATAR_PATH for candidate in candidates)
    assert all(urlsplit(candidate).query == "size=184" for candidate in candidates)
    assert all(urlsplit(candidate).fragment == "avatar" for candidate in candidates)


@pytest.mark.parametrize("url", [
    "https://example.com/avatar.jpg",
    "https://img-eshop.cdn.nintendo.net/i/game.jpg",
    "https://image.api.playstation.com/avatar.jpg",
    "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/570/header.jpg",
    "https://avatars.steamstatic.com.example.com/avatar.jpg",
    "https://user@avatars.steamstatic.com/avatar.jpg",
    "http://avatars.steamstatic.com/avatar.jpg",
])
def test_other_hosts_and_steam_game_images_keep_original_download_behavior(monkeypatch, url):
    download = AsyncMock(return_value=b"unchanged")
    monkeypatch.setattr(presence, "_download_image_single", download)
    assert asyncio.run(presence._download_image(url)) == b"unchanged"
    download.assert_awaited_once_with(url)


def test_successful_original_does_not_request_backup_hosts(monkeypatch):
    content = avatar_bytes()
    download = AsyncMock(return_value=content)
    monkeypatch.setattr(presence, "_download_image_single", download)
    assert asyncio.run(presence._download_image(ORIGINAL)) == content
    download.assert_awaited_once_with(ORIGINAL)


@pytest.mark.parametrize("failures,expected_urls", [
    (1, [ORIGINAL, AKAMAI]),
    (2, [ORIGINAL, AKAMAI, FASTLY]),
])
def test_failed_hosts_fall_back_in_order(monkeypatch, failures, expected_urls):
    content = avatar_bytes()
    download = AsyncMock(side_effect=[*(httpx.ConnectError("reset") for _ in range(failures)), content])
    monkeypatch.setattr(presence, "_download_image_single", download)
    assert asyncio.run(presence._download_image(ORIGINAL)) == content
    assert [call.args[0] for call in download.await_args_list] == expected_urls


@pytest.mark.parametrize("bad_response", [None, b"<html>blocked</html>"])
def test_invalid_image_response_also_tries_backup(monkeypatch, bad_response):
    content = avatar_bytes()
    download = AsyncMock(side_effect=[bad_response, content])
    monkeypatch.setattr(presence, "_download_image_single", download)
    assert asyncio.run(presence._download_image(ORIGINAL)) == content
    assert [call.args[0] for call in download.await_args_list] == [ORIGINAL, AKAMAI]


def test_slow_original_and_first_backup_leave_time_for_last_backup(monkeypatch):
    content = avatar_bytes()
    attempted, cancelled = [], []
    async def download(url):
        attempted.append(url)
        if url == FASTLY:
            return content
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.append(url)
    monkeypatch.setattr(presence, "_download_image_single", download)
    monkeypatch.setattr(presence, "STEAM_AVATAR_ROUTE_TIMEOUT", .01)
    assert asyncio.run(presence._download_image(ORIGINAL)) == content
    assert attempted == [ORIGINAL, AKAMAI, FASTLY]
    assert cancelled == [ORIGINAL, AKAMAI]


def test_all_hosts_failed_propagates_last_failure(monkeypatch):
    download = AsyncMock(side_effect=httpx.ConnectError("all reset"))
    monkeypatch.setattr(presence, "_download_image_single", download)
    with pytest.raises(httpx.ConnectError, match="all reset"):
        asyncio.run(presence._download_image(ORIGINAL))
    assert [call.args[0] for call in download.await_args_list] == [ORIGINAL, AKAMAI, FASTLY]


def test_external_cancellation_does_not_start_more_requests(monkeypatch):
    attempted = []
    async def download(url):
        attempted.append(url)
        raise asyncio.CancelledError
    monkeypatch.setattr(presence, "_download_image_single", download)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(presence._download_image(ORIGINAL))
    assert attempted == [ORIGINAL]

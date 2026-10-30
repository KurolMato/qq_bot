import asyncio
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from qq_bot import dsh_web_images as web

PNG = b"\x89PNG\r\n\x1a\n" + b"image-data"


def test_markers_are_stripped_deduplicated_and_limited():
    text = "找到了\n" + "".join(f"[[发送图片:https://example.test/{i}.png]]" for i in (1, 1, 2, 3, 4))
    cleaned, urls, requested = web.split_web_image_markers(text)
    assert cleaned == "找到了"
    assert urls == [f"https://example.test/{i}.png" for i in (1, 2, 3)]
    assert requested


@pytest.mark.parametrize("text", ["[[发送图片:https://example.test/a", "［［发图 没有链接］］"])
def test_malformed_markers_do_not_leak(text):
    assert web.split_web_image_markers("说明\n" + text) == ("说明", [], True)


@pytest.mark.parametrize("url,address,allowed", [
    ("https://example.test/a", "8.8.8.8", True),
    ("https://example.test/a", "192.168.1.2", False),
    ("http://127.0.0.1/a", "127.0.0.1", False),
    ("http://[::1]/a", "::1", False),
    ("file:///C:/secret.png", None, False),
    ("https://user:password@example.test/a", None, False),
])
def test_public_destination_check(monkeypatch, url, address, allowed):
    async def run():
        lookup = AsyncMock(return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))])
        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", lookup)
        assert await web._public_url(httpx.URL(url)) is allowed
        assert lookup.await_count == (1 if address else 0)
    asyncio.run(run())


def test_mixed_public_and_private_dns_is_rejected(monkeypatch):
    async def run():
        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", AsyncMock(return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443)) for ip in ("8.8.8.8", "10.0.0.1")
        ]))
        assert not await web._public_url(httpx.URL("https://example.test/a"))
    asyncio.run(run())


@pytest.fixture
def network(monkeypatch):
    requests = []
    responses = []
    def handle(request):
        requests.append(request)
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response
    factory = httpx.AsyncClient
    monkeypatch.setattr(web.httpx, "AsyncClient", lambda **kwargs: factory(
        transport=httpx.MockTransport(handle), **kwargs))
    monkeypatch.setattr(web, "_public_url", AsyncMock(return_value=True))
    return requests, responses


def test_download_follows_public_redirect_and_checks_actual_image(network):
    requests, responses = network
    responses.extend([httpx.Response(302, headers={"location": "/real.png"}), httpx.Response(200, content=PNG)])
    assert asyncio.run(web.download_web_image("https://example.test/a")) == PNG
    assert [str(req.url) for req in requests] == ["https://example.test/a", "https://example.test/real.png"]
    assert web._public_url.await_count == 2


def test_redirect_to_local_address_is_never_fetched(network, monkeypatch):
    requests, responses = network
    responses.append(httpx.Response(302, headers={"location": "http://127.0.0.1/private"}))
    monkeypatch.setattr(web, "_public_url", AsyncMock(side_effect=[True, False]))
    assert asyncio.run(web.download_web_image("https://example.test/a")) is None
    assert len(requests) == 1


@pytest.mark.parametrize("response", [
    httpx.Response(200, content=b"<html>login required</html>"),
    httpx.Response(200, content=PNG, headers={"content-length": str(web.MAX_IMAGE_BYTES + 1)}),
    httpx.Response(403),
    httpx.ReadError("connection failed"),
])
def test_invalid_download_returns_none(network, response):
    network[1].append(response)
    assert asyncio.run(web.download_web_image("https://example.test/a")) is None


def test_stream_size_is_limited_without_content_length(network, monkeypatch):
    monkeypatch.setattr(web, "MAX_IMAGE_BYTES", 10)
    network[1].append(httpx.Response(200, content=PNG))
    assert asyncio.run(web.download_web_image("https://example.test/a")) is None


def test_unreachable_dns_is_a_download_failure(monkeypatch):
    async def run():
        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", AsyncMock(side_effect=OSError("DNS failed")))
        assert await web.download_web_image("https://example.test/a") is None
    asyncio.run(run())


def test_download_has_total_time_limit(monkeypatch):
    async def timeout(url):
        raise asyncio.TimeoutError()
    monkeypatch.setattr(web, "_download", timeout)
    assert asyncio.run(web.download_web_image("https://example.test/a")) is None


def test_delivery_reports_partial_failure_and_keeps_sending(monkeypatch):
    monkeypatch.setattr(web, "download_web_image", AsyncMock(side_effect=[PNG, PNG, None]))
    bot = SimpleNamespace(send=AsyncMock(side_effect=[RuntimeError("QQ failure"), None, None]))
    assert asyncio.run(web.send_web_images(bot, object(), ["a", "b", "c"])) == 1
    assert "另外 2 张" in bot.send.await_args.args[1]
    segment = bot.send.await_args_list[1].args[1]
    assert segment.type == "image"
    assert "sub_type" not in segment.data


def test_delivery_reports_total_failure(monkeypatch):
    monkeypatch.setattr(web, "download_web_image", AsyncMock(return_value=None))
    bot = SimpleNamespace(send=AsyncMock())
    assert asyncio.run(web.send_web_images(bot, object(), ["a"])) == 0
    assert "图片没发出来" in bot.send.await_args.args[1]

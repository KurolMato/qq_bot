import asyncio
from io import BytesIO

import httpx
from PIL import Image

from tools import diagnose_steam_avatar as diagnostic


def test_probe_requires_an_actual_image_and_reports_connection_cause(monkeypatch):
    output = BytesIO()
    Image.new("RGB", (2, 2)).save(output, "PNG")
    responses = [output.getvalue(), b"<html>proxy login</html>", None]
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get(self, url):
            content = responses.pop(0)
            if content is None:
                try:
                    raise OSError("certificate verify failed")
                except OSError as exc:
                    raise httpx.ConnectError("connection failed") from exc
            return httpx.Response(200, content=content, request=httpx.Request("GET", url))
    monkeypatch.setattr(diagnostic.httpx, "AsyncClient", Client)
    async def run():
        return [await diagnostic.probe("test", "https://example.com/avatar", trust_env=False) for _ in range(3)]
    good, html, failed = asyncio.run(run())
    assert good[1] is True
    assert html[1] is False
    assert failed[1] is False
    assert "certificate verify failed" in failed[2]


def test_error_output_redacts_proxy_password():
    message = diagnostic.error_chain(httpx.ConnectError("Cannot connect to http://user:password@127.0.0.1:7890"))
    assert "password" not in message
    assert "user:" not in message


def test_diagnostic_compares_proxy_and_certificate_combinations(monkeypatch):
    options = []
    async def probe(label, url, **kwargs):
        options.append(kwargs)
        return label, True, "valid image"
    monkeypatch.setattr(diagnostic, "probe", probe)
    monkeypatch.setenv("STEAM_HTTP_PROXY", "http://127.0.0.1:7890")
    assert len(asyncio.run(diagnostic.diagnose("https://example.com/avatar"))) == 6
    assert [(item["trust_env"], item.get("system_cert", False)) for item in options[:4]] == [
        (False, False), (True, False), (False, True), (True, True),
    ]
    assert all(item["proxy"] == "http://127.0.0.1:7890" for item in options[4:])

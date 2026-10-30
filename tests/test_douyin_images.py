import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
from PIL import Image

from app import downloader
from tools.douyin_note_metadata import fetch_metadata, read_netscape_cookie_file


def test_direct_note_keeps_original_images_and_cleans_cookie(tmp_path, monkeypatch):
    cookie = tmp_path / "cookies.txt"
    cookie.write_text("cookie", encoding="utf-8")
    job = tmp_path / "job"
    job.mkdir()
    urls = ["https://p3.douyinpic.com/a", "https://p3.douyinpic.com/b"]
    monkeypatch.setattr(downloader, "_douyin_cookie_file", lambda: cookie)
    monkeypatch.setattr(downloader, "_resolve_douyin_detail", lambda *args: {"images": [{"url_list": [url]} for url in urls], "music": {"play_url": {"url_list": ["https://music.invalid"]}}})
    originals = []

    def asset(url, output, timeout):
        assert url == urls[len(originals)]
        Image.new("RGB", (300 + len(originals), 200), "orange").save(output, "JPEG")
        originals.append(output.read_bytes())

    monkeypatch.setattr(downloader, "_download_douyin_asset", asset)
    with patch.object(downloader, "_run_subprocess") as run:
        images, logs = downloader._download_douyin_note_images("https://www.douyin.com/note/12345", job, 30)
    assert [image.read_bytes() for image in images] == originals
    assert [Image.open(image).size for image in images] == [(300, 200), (301, 200)]
    assert not (job / "douyin-cookies.txt").exists()
    assert cookie.read_text() == "cookie"
    assert "2 张" in logs
    run.assert_not_called()


def test_note_detail_uses_lowercase_argus_and_checks_item_id(tmp_path):
    detail = {"aweme_id": "12345", "images": [{"url_list": ["https://p3.douyinpic.com/a"]}]}
    response = httpx.Response(200, json={"aweme_detail": detail}, request=httpx.Request("GET", "https://www.douyin.com/"))
    with patch.object(downloader, "httpx") as client, patch.object(downloader, "_find_douyin_cookie_file", return_value=None):
        client.get.return_value = response
        result = downloader._resolve_douyin_detail("https://www.douyin.com/note/12345", tmp_path, 30)
        assert result == detail
        assert client.get.call_args.kwargs["headers"]["x-tt-argus"] == "1"
        assert client.get.call_args.kwargs["params"] == {"aweme_id": "12345"}


def test_images_keep_order_on_partial_failure_and_convert_webp(tmp_path, monkeypatch):
    def asset(url, output, timeout):
        if url == "bad":
            raise httpx.ConnectError("reset with secret URL")
        Image.new("RGBA", (50, 70), "red").save(output, "WEBP")
    monkeypatch.setattr(downloader, "_download_douyin_asset", asset)
    images, logs = downloader._download_douyin_images(["first", "bad", "last"], tmp_path, 30)
    assert [image.name for image in images] == ["image-000.jpg", "image-002.jpg"]
    assert Image.open(images[0]).size == (50, 70)
    assert "第 2 张" in logs
    assert "secret" not in logs


def test_failed_f2_does_not_expose_signed_request_and_cleans_cookie(tmp_path, monkeypatch):
    cookie = tmp_path / "original.txt"
    cookie.write_text("cookie")
    job = tmp_path / "job"
    job.mkdir()
    monkeypatch.setattr(downloader, "_douyin_cookie_file", lambda: cookie)
    monkeypatch.setattr(downloader, "_douyin_f2_python", lambda: tmp_path / "python.exe")
    with patch.object(downloader, "_resolve_douyin_detail", side_effect=downloader.DownloadError("no detail")), patch.object(downloader, "_run_subprocess", return_value=Mock(returncode=1, stdout="", stderr="403 URL msToken=secret&a_bogus=signature")):
        with pytest.raises(downloader.DownloadError, match="HTTP 403") as error:
            downloader._download_douyin_note_images("https://www.douyin.com/note/12345", job, 30)
    assert "secret" not in str(error.value)
    assert "a_bogus" not in str(error.value)
    assert not (job / "douyin-cookies.txt").exists()


def test_worker_ignores_expired_cookies_and_sets_argus(tmp_path, monkeypatch):
    cookie = tmp_path / "cookies.txt"
    cookie.write_text("# Netscape HTTP Cookie File\n.douyin.com\tTRUE\t/\tTRUE\t1\texpired\told\n.douyin.com\tTRUE\t/\tTRUE\t1999999999\tsessionid\ttest\n.evil.test\tTRUE\t/\tTRUE\t1999999999\tother\twrong\n", encoding="utf-8")
    assert read_netscape_cookie_file(cookie) == "sessionid=test"
    raw_images = [{"url_list": ["https://p3.douyinpic.com/a"]}, {"url_list": ["https://p3.douyinpic.com/b"], "video": {"play_addr": {"url_list": ["https://v1.douyinvod.com/live.mp4"]}}}]
    handler = Mock(return_value=SimpleNamespace(fetch_one_video=AsyncMock(return_value=SimpleNamespace(aweme_id="12345", aweme_type=68, images=["https://p3.douyinpic.com/a"], music_play_url=None, _to_raw=lambda: {"aweme_detail": {"images": raw_images}}))))
    monkeypatch.setitem(sys.modules, "f2.apps.douyin.handler", SimpleNamespace(DouyinHandler=handler))
    monkeypatch.setitem(sys.modules, "f2.apps.douyin.utils", SimpleNamespace(AwemeIdFetcher=SimpleNamespace(get_aweme_id=AsyncMock(return_value="12345"))))
    result = asyncio.run(fetch_metadata("https://www.douyin.com/note/12345", cookie))
    assert result["images"] == ["https://p3.douyinpic.com/a"]
    assert result["image_details"] == raw_images
    assert handler.call_args.args[0]["headers"]["x-tt-argus"] == "1"

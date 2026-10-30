from unittest.mock import patch
from urllib.parse import parse_qs, quote, urlparse

import httpx
import pytest

from app.downloader import (
    DownloadError, _recover_xiaohongshu_note_url, resolve_xiaohongshu_share_url,
)


SHORT = "https://xhslink.cn/o/8yab3Ulqx46"
NOTE = "https://www.xiaohongshu.com/discovery/item/6aa804a40000000011034535?xsec_token=demo%2Bvalue&xsec_source=app_share&app_platform=ios&share_id=example&track_code=normal"


def response(url, status=200, history=(), headers=None):
    return httpx.Response(status, request=httpx.Request("GET", url), history=list(history), headers=headers)


def test_mobile_short_request_is_preferred():
    with patch("app.downloader.httpx.get", return_value=response(NOTE)) as get:
        assert resolve_xiaohongshu_share_url(SHORT) == NOTE
    assert "iPhone" in get.call_args.kwargs["headers"]["User-Agent"]


def test_login_redirect_path_preserves_full_share_signature():
    login = "https://www.xiaohongshu.com/login?redirectPath=" + quote(urlparse(NOTE).path + "?" + urlparse(NOTE).query, safe="")
    with patch("app.downloader.httpx.get", return_value=response(login)):
        result = resolve_xiaohongshu_share_url(SHORT)
    assert result == NOTE
    assert parse_qs(urlparse(result).query)["xsec_token"] == ["demo+value"]


@pytest.mark.parametrize("status", [200, 404])
def test_note_is_recovered_from_history_even_if_final_landing_has_no_id(status):
    steps = [response(SHORT, 302, headers={"location": NOTE}), response(NOTE, 302, headers={"location": "/login"})]
    with patch("app.downloader.httpx.get", return_value=response("https://www.xiaohongshu.com/login", status, steps)):
        assert resolve_xiaohongshu_share_url(SHORT) == NOTE


def test_desktop_retry_after_mobile_landing_has_no_note():
    with patch("app.downloader.httpx.get", side_effect=[response("https://www.xiaohongshu.com/explore"), response(NOTE)]) as get:
        assert resolve_xiaohongshu_share_url(SHORT) == NOTE
    assert get.call_count == 2
    assert "Windows" in get.call_args.kwargs["headers"]["User-Agent"]


def test_untrusted_redirect_is_not_hidden_by_note_history():
    steps = [response(SHORT, 302, headers={"location": NOTE})]
    with patch("app.downloader.httpx.get", return_value=response("https://evil.example/explore/6aa804a40000000011034535", history=steps)):
        with pytest.raises(DownloadError, match="不受信任"):
            resolve_xiaohongshu_share_url(SHORT)
    assert _recover_xiaohongshu_note_url("https://www.xiaohongshu.com/login?redirectPath=" + quote("https://evil.example/explore/6aa804a40000000011034535", safe="")) is None


def test_transport_failure_keeps_original_short_link():
    with patch("app.downloader.httpx.get", side_effect=httpx.ConnectError("reset")) as get:
        assert resolve_xiaohongshu_share_url(SHORT) == SHORT
    assert get.call_count == 2


def test_login_without_note_address_reports_login_instead_of_motion_format():
    with patch("app.downloader.httpx.get", return_value=response("https://www.xiaohongshu.com/login")):
        with pytest.raises(DownloadError, match="登录页"):
            resolve_xiaohongshu_share_url(SHORT)


def test_mobile_video_note_is_not_sent_as_its_cover(tmp_path, monkeypatch):
    from app import downloader
    note = {"type": "video", "imageList": [{"urlDefault": "https://sns-img.xhscdn.com/cover.jpg"}], "video": {"media": {"stream": {"h264": [{"masterUrl": "https://sns-video.xhscdn.com/video.mp4", "backupUrls": ["https://sns-video.xhscdn.com/backup.mp4"]}]}}}}
    monkeypatch.setattr(downloader, "_resolve_xiaohongshu_note", lambda *args: note)
    with patch.object(downloader, "_album_result", return_value=(tmp_path / "video.mp4", "converted")) as convert:
        result, logs = downloader._download_xiaohongshu_album(NOTE, tmp_path, 30)
    assert result.name == "video.mp4"
    item = convert.call_args.args[0][0]
    assert item.video_urls == ("https://sns-video.xhscdn.com/video.mp4", "https://sns-video.xhscdn.com/backup.mp4")
    assert item.image_urls == ()
    assert "恢复小红书视频" in logs


def test_inaccessible_video_note_never_falls_back_to_cover(tmp_path, monkeypatch):
    from app import downloader
    monkeypatch.setattr(downloader, "_resolve_xiaohongshu_note", lambda *args: {"type": "video", "video": {}, "imageList": [{"urlDefault": "cover"}]})
    with pytest.raises(DownloadError, match="未返回可用播放地址"):
        downloader._download_xiaohongshu_album(NOTE, tmp_path, 30)

from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
import pytest

from app import downloader


SHORT = "https://v.douyin.com/jqT-BdpUyM0/"
VIDEO = "https://www.douyin.com/video/7695582229237452083?source=share"


def redirect(target):
    return httpx.Response(302, request=httpx.Request("GET", SHORT), headers={"location": target})


def test_short_link_stops_before_share_page_tls_connection():
    with patch.object(downloader.httpx, "get", return_value=redirect("https://www.iesdouyin.com/share/video/7695582229237452083/?source=share")) as get:
        assert downloader.resolve_douyin_share_url(SHORT) == VIDEO
    assert get.call_count == 1
    assert get.call_args.kwargs["follow_redirects"] is False


def test_short_link_recovers_after_ssl_connection_interruption():
    with patch.object(downloader.httpx, "get", side_effect=[httpx.ConnectError("SSL EOF"), redirect(VIDEO)]) as get:
        assert downloader.resolve_douyin_share_url(SHORT) == VIDEO
    assert get.call_count == 2
    assert get.call_args_list[0].kwargs["headers"] != get.call_args_list[1].kwargs["headers"]


def test_short_link_retries_are_bounded_and_share_time_budget():
    with patch.object(downloader.httpx, "get", side_effect=httpx.ConnectError("SSL EOF")) as get:
        assert downloader.resolve_douyin_share_url(SHORT, 10) == SHORT
    assert get.call_count == 3
    budgets = [call.kwargs["timeout"] for call in get.call_args_list]
    assert 0 < budgets[-1] <= budgets[0] <= 10


def test_short_link_rejects_external_redirect_before_connecting():
    with patch.object(downloader.httpx, "get", return_value=redirect("https://evil.example/video/12345")) as get:
        with pytest.raises(downloader.DownloadError, match="不受信任"):
            downloader.resolve_douyin_share_url(SHORT)
    assert get.call_count == 1


@pytest.mark.parametrize("failure", ["[SSL: UNEXPECTED_EOF_WHILE_READING]", "ConnectionResetError(10054)", "Read timed out"])
def test_downloader_ssl_failure_uses_compatible_detail_once(tmp_path, monkeypatch, failure):
    job = tmp_path / "job"
    monkeypatch.setattr(downloader, "resolve_douyin_share_url", lambda url, timeout: url)
    monkeypatch.setattr(downloader, "ensure_runtime", lambda platform: ["python", "-m", "yt_dlp"])
    monkeypatch.setattr(downloader, "_douyin_cookie_arguments", lambda *args: [])
    media = "https://v1.douyinvod.com/public.mp4"
    def run(args, **kwargs):
        if args[-1] == media:
            (job / "download.mp4").write_bytes(b"video")
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr=failure)
    with patch.object(downloader, "_run_subprocess", side_effect=run) as download, patch.object(downloader, "_resolve_douyin_video_media_candidates", return_value=[media]) as detail:
        output, _ = downloader.download_to_mp4(SHORT, job, 30)
    assert output.read_bytes() == b"video"
    detail.assert_called_once()
    assert download.call_count == 2
    assert download.call_args_list[1].kwargs["timeout"] <= download.call_args_list[0].kwargs["timeout"] <= 30
    assert "--ignore-config" in download.call_args.args[0]
    assert "--cookies" not in download.call_args.args[0]


def test_ssl_recovery_can_detect_live_album_in_video_alias(tmp_path, monkeypatch):
    monkeypatch.setattr(downloader, "resolve_douyin_share_url", lambda url, timeout: url)
    monkeypatch.setattr(downloader, "ensure_runtime", lambda platform: ["python", "-m", "yt_dlp"])
    monkeypatch.setattr(downloader, "_douyin_cookie_arguments", lambda *args: [])
    detail = {"images": [{"url_list": ["cover"], "video": {"play_addr": {"url_list": ["https://v1.douyinvod.com/live.mp4"]}}}]}
    result = tmp_path / "job" / "live-photo.mp4"
    with patch.object(downloader, "_run_subprocess", return_value=SimpleNamespace(returncode=1, stdout="", stderr="SSL: UNEXPECTED_EOF_WHILE_READING")), patch.object(downloader, "_resolve_douyin_video_media_candidates", side_effect=downloader._DouyinAlbum(detail)), patch.object(downloader, "_album_result", return_value=(result, "live")) as album:
        assert downloader.download_to_mp4(SHORT, result.parent, 30) == (result, "live")
    assert album.call_args.args[0][0].is_live


def test_repeated_ssl_failure_has_clear_error_and_cleans_cookie(tmp_path, monkeypatch):
    job = tmp_path / "job"
    monkeypatch.setattr(downloader, "resolve_douyin_share_url", lambda url, timeout: url)
    monkeypatch.setattr(downloader, "ensure_runtime", lambda platform: ["python", "-m", "yt_dlp"])
    def cookie(*args):
        (job / "douyin-cookies.txt").write_text("temporary")
        return []
    monkeypatch.setattr(downloader, "_douyin_cookie_arguments", cookie)
    with patch.object(downloader, "_run_subprocess", return_value=SimpleNamespace(returncode=1, stdout="", stderr="SSL: UNEXPECTED_EOF_WHILE_READING")) as run, patch.object(downloader, "_resolve_douyin_video_media_candidates", side_effect=downloader.DownloadError("无法连接")):
        with pytest.raises(downloader.DownloadError, match="兼容详情解析失败：无法连接"):
            downloader.download_to_mp4(SHORT, job, 30)
    assert run.call_count == 1
    assert not (job / "douyin-cookies.txt").exists()


def test_diagnostic_does_not_leak_signed_urls_or_cookies(caplog):
    secret = "SENSITIVE_COOKIE_AND_SIGNATURE"
    downloader._log_douyin_failure(
        "media_download", f"https://v1.douyinvod.com/video?sign={secret}",
        f"SSL: UNEXPECTED_EOF_WHILE_READING Cookie: {secret}", "temporary-job",
    )
    assert "stage=media_download" in caplog.text
    assert "host=v1.douyinvod.com" in caplog.text
    assert "kinds=ssl" in caplog.text
    assert secret not in caplog.text
    assert "?sign=" not in caplog.text


def test_initial_ssl_error_does_not_hide_compatible_http403(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(downloader, "resolve_douyin_share_url", lambda url, timeout: VIDEO)
    monkeypatch.setattr(downloader, "ensure_runtime", lambda platform: ["python", "-m", "yt_dlp"])
    monkeypatch.setattr(downloader, "_douyin_cookie_arguments", lambda *args: [])
    response = httpx.Response(403, request=httpx.Request("GET", "https://www.douyin.com/?secret=hidden"))
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as cause:
        error = downloader.DownloadError("抖音详情接口拒绝访问（HTTP 403）")
        error.__cause__ = cause
    with patch.object(downloader, "_run_subprocess", return_value=SimpleNamespace(returncode=1, stdout="", stderr="SSL: UNEXPECTED_EOF_WHILE_READING")), patch.object(downloader, "_resolve_douyin_video_media_candidates", side_effect=error):
        with pytest.raises(downloader.DownloadError, match="兼容详情解析失败：.*HTTP 403"):
            downloader.download_to_mp4(SHORT, tmp_path / "job", 30)
    assert "stage=extractor" in caplog.text
    assert "stage=compatible_detail" in caplog.text
    assert "kinds=http403" in caplog.text
    assert "secret=hidden" not in caplog.text


@pytest.mark.parametrize("failure", [httpx.ConnectError("SSL EOF"), 503])
def test_detail_recovers_from_transient_failure(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(downloader, "_find_douyin_cookie_file", lambda: None)
    request = httpx.Request("GET", "https://www.douyin.com/aweme/v1/web/aweme/detail/")
    if isinstance(failure, int):
        failure = httpx.Response(failure, request=request)
    ok = httpx.Response(200, request=request, json={"aweme_detail": {"aweme_id": "12345"}})
    with patch.object(downloader.httpx, "get", side_effect=[failure, ok]) as get:
        assert downloader._resolve_douyin_detail("https://www.douyin.com/video/12345", tmp_path, 30)["aweme_id"] == "12345"
    assert get.call_count == 2
    assert 0 < get.call_args.kwargs["timeout"] <= 10


def test_detail_refusal_is_not_retried(tmp_path, monkeypatch):
    monkeypatch.setattr(downloader, "_find_douyin_cookie_file", lambda: None)
    response = httpx.Response(403, request=httpx.Request("GET", VIDEO))
    with patch.object(downloader.httpx, "get", return_value=response) as get:
        with pytest.raises(downloader.DownloadError, match="HTTP 403"):
            downloader._resolve_douyin_detail(VIDEO, tmp_path, 30)
    assert get.call_count == 1


def test_detail_retries_stop_at_three_and_total_deadline():
    import http.cookiejar
    with patch.object(downloader.httpx, "get", side_effect=httpx.ConnectError("SSL EOF")) as get:
        with pytest.raises(httpx.ConnectError):
            downloader._request_douyin_detail("12345", http.cookiejar.CookieJar(), 30)
    assert get.call_count == 3
    with patch.object(downloader.time, "monotonic", side_effect=[0, 0, 31]), patch.object(downloader.httpx, "get", side_effect=httpx.ConnectError("SSL EOF")) as get:
        with pytest.raises(httpx.ReadTimeout):
            downloader._request_douyin_detail("12345", http.cookiejar.CookieJar(), 30)
    assert get.call_count == 1


def test_media_candidates_are_trusted_unique_and_bounded(tmp_path, monkeypatch):
    urls = ["https://evil.example/video", *[f"https://v{i}.douyinvod.com/video" for i in range(5)]]
    detail = {"video": {"play_addr_h264": {"url_list": [urls[1], *urls]}, "play_addr": {"url_list": [urls[-1]]}}}
    monkeypatch.setattr(downloader, "_resolve_douyin_detail", lambda *args: detail)
    assert downloader._resolve_douyin_video_media_candidates(VIDEO, tmp_path, 30) == urls[1:4]


def test_failed_media_node_switches_to_backup_without_credentials(tmp_path, monkeypatch):
    job = tmp_path / "job"
    media = ["https://v1.douyinvod.com/first", "https://v2.douyinvod.com/backup", "https://v3.douyinvod.com/unused"]
    monkeypatch.setattr(downloader, "resolve_douyin_share_url", lambda url, timeout: VIDEO)
    monkeypatch.setattr(downloader, "ensure_runtime", lambda platform: ["python", "-m", "yt_dlp"])
    monkeypatch.setattr(downloader, "_douyin_cookie_arguments", lambda *args: ["--cookies", "temporary-cookie"])
    def run(args, **kwargs):
        if args[-1] == media[1]:
            (job / "douyin.mp4").write_bytes(b"complete-video")
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="SSL: UNEXPECTED_EOF_WHILE_READING")
    with patch.object(downloader, "_run_subprocess", side_effect=run) as execute, patch.object(downloader, "_resolve_douyin_video_media_candidates", return_value=media) as detail:
        output, _ = downloader.download_to_mp4(SHORT, job, 40)
    assert output.read_bytes() == b"complete-video"
    detail.assert_called_once()
    assert [call.args[0][-1] for call in execute.call_args_list] == [VIDEO, *media[:2]]
    for call in execute.call_args_list[1:]:
        assert "--cookies" not in call.args[0]
        assert "--no-continue" in call.args[0]
        assert "--force-overwrites" in call.args[0]
    budgets = [call.kwargs["timeout"] for call in execute.call_args_list]
    assert 0 < budgets[-1] <= budgets[0] <= 40

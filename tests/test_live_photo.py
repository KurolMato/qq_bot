import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
import pytest
from PIL import Image

from app import downloader
from app.live_photo import (
    AlbumItem, LivePhotoError, douyin_album, render_live_album,
    xiaohongshu_album, xiaohongshu_note,
)


def test_douyin_album_preserves_static_live_order_and_backups():
    items = douyin_album({"images": [
        {"url_list": ["cover1", "cover1b"]},
        {"url_list": ["cover2"], "video": {"play_addr_h264": {"url_list": ["h264", "backup"]}, "play_addr": {"url_list": ["h265"]}}},
        {"url_list": ["cover3"], "is_live_photo": True},
    ]})
    assert [item.is_live for item in items] == [False, True, True]
    assert items[1].video_urls == ("h264", "backup", "h265")
    assert items[2].video_urls == ()  # Never silently replace missing motion with a still.


def test_xiaohongshu_web_and_mobile_live_streams():
    items = xiaohongshu_album({"imageList": [
        {"urlDefault": "still"},
        {"urlDefault": "cover", "livePhoto": True, "stream": {"h264": [{"masterUrl": "primary", "backupUrls": ["backup"]}], "h265": [{"masterUrl": "hevc"}]}},
        {"urlDefault": "cover2", "livePhoto": {"media": {"stream": {"h265": [{"master_url": "mobile", "backup_urls": ["mobile-backup"]}]}}}},
    ]})
    assert [item.is_live for item in items] == [False, True, True]
    assert items[1].video_urls == ("primary", "backup", "hevc")
    assert items[2].video_urls == ("mobile", "mobile-backup")
    note = {"noteId": "abc", "imageList": []}
    assert xiaohongshu_note({"noteData": {"data": {"noteData": note}}}, "abc") == note
    with pytest.raises(LivePhotoError, match="其他笔记"):
        xiaohongshu_note({"noteData": {"data": {"noteData": note}}}, "wrong")


def test_xhs_initial_state_keeps_share_parameters_and_parses_js(tmp_path, monkeypatch):
    from yt_dlp.extractor.xiaohongshu import XiaoHongShuIE
    note_id = "674051740000000007027a15"
    note = {"noteId": note_id, "imageList": [{"livePhoto": True, "stream": {"h264": [{"masterUrl": "https://sns-video.xhscdn.com/live.mp4"}]}}]}
    state = {"note": {"noteDetailMap": {note_id: {"note": note}}}}
    html = "<script>window.__INITIAL_STATE__=" + json.dumps(state)[:-1] + ',"empty":undefined,"map":new Map([])}</script>'
    monkeypatch.setattr(downloader, "_xiaohongshu_cookie_arguments", lambda *args: [])
    url = f"https://www.xiaohongshu.com/explore/{note_id}?xsec_token=demo&xsec_source=app_share"
    with patch.object(XiaoHongShuIE, "_download_webpage", return_value=html) as fetch:
        assert downloader._resolve_xiaohongshu_note(url, tmp_path, 30) == note
    assert fetch.call_args.args[0] == url


def test_xhs_cdn_rejects_external_redirect_before_request(tmp_path):
    response = httpx.Response(302, headers={"location": "https://untrusted.example/private"})
    context = Mock()
    context.__enter__ = Mock(return_value=response)
    context.__exit__ = Mock(return_value=False)
    with patch.object(downloader.httpx, "stream", return_value=context) as stream:
        with pytest.raises(downloader.DownloadError, match="不受信任"):
            downloader._download_xiaohongshu_asset("//sns-video.xhscdn.com/live.mp4", tmp_path / "clip", 10)
    assert stream.call_count == 1
    assert "cookies" not in stream.call_args.kwargs
    with pytest.raises(downloader.DownloadError):
        downloader._trusted_xiaohongshu_asset_url("https://xhscdn.com.evil.test/live.mp4")


def test_douyin_note_routes_to_live_renderer_and_cleans_cookie(tmp_path, monkeypatch):
    cookie = tmp_path / "original.txt"
    cookie.write_text("cookie")
    job = tmp_path / "job"
    job.mkdir()
    monkeypatch.setattr(downloader, "_douyin_cookie_file", lambda: cookie)
    monkeypatch.setattr(downloader, "_resolve_douyin_detail", lambda *args: {"images": [{"url_list": ["cover"], "video": {"play_addr": {"url_list": ["https://v1.douyinvod.com/live.mp4"]}}}]})
    output = job / "live-photo.mp4"
    with patch.object(downloader, "render_live_album", return_value=output) as render:
        result, logs = downloader._download_douyin_note_images("https://www.douyin.com/note/12345", job, 30)
    assert result == output
    assert render.call_args.args[0][0].is_live
    assert "实况 MP4" in logs
    assert cookie.read_text() == "cookie"
    assert not (job / "douyin-cookies.txt").exists()


def test_douyin_video_alias_detects_album(tmp_path, monkeypatch):
    detail = {"images": [{"url_list": ["cover"], "video": {"play_addr": {"url_list": ["live"]}}}]}
    monkeypatch.setattr(downloader, "_resolve_douyin_detail", lambda *args: detail)
    with pytest.raises(downloader._DouyinAlbum) as error:
        downloader._resolve_douyin_video_media("https://www.douyin.com/video/12345", tmp_path, 30)
    assert error.value.detail == detail


def test_xhs_no_formats_uses_live_album_and_cleans_task_cookie(tmp_path, monkeypatch):
    job = tmp_path / "job"
    note_id = "674051740000000007027a15"
    output = job / "live-photo.mp4"
    monkeypatch.setattr(downloader, "ensure_runtime", lambda *args: ["python", "-m", "yt_dlp"])
    monkeypatch.setattr(downloader, "_xiaohongshu_cookie_arguments", lambda *args: [])
    monkeypatch.setattr(downloader, "_run_subprocess", lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr="No video formats found!"))
    def album(url, directory, timeout):
        assert url == f"https://www.xiaohongshu.com/explore/{note_id}"
        (directory / "xiaohongshu-cookies.txt").write_text("task cookie")
        output.write_bytes(b"mp4")
        return output, "live"
    monkeypatch.setattr(downloader, "_download_xiaohongshu_album", album)
    assert downloader.download_to_mp4(f"https://www.xiaohongshu.com/explore/{note_id}", job, 30) == (output, "live")
    assert not (job / "xiaohongshu-cookies.txt").exists()


def test_missing_motion_fails_instead_of_using_cover(tmp_path):
    with pytest.raises(LivePhotoError, match="未提供视频地址"):
        render_live_album([AlbumItem(("cover",), (), True)], tmp_path, 30, Mock())


def test_f2_fallback_keeps_live_position_in_mixed_note(tmp_path, monkeypatch):
    job = tmp_path / "job"
    job.mkdir()
    cookie = tmp_path / "cookie.txt"
    cookie.write_text("cookie")
    monkeypatch.setattr(downloader, "_douyin_cookie_file", lambda: cookie)
    monkeypatch.setattr(downloader, "_douyin_f2_python", lambda: tmp_path / "python.exe")
    def run(*args, **kwargs):
        (job / "note.json").write_text(json.dumps({"image_details": [
            {"url_list": ["still"]},
            {"url_list": ["cover"], "video": {"play_addr": {"url_list": ["live"]}}},
        ]}))
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(downloader, "_run_subprocess", run)
    with patch.object(downloader, "_resolve_douyin_detail", side_effect=downloader.DownloadError("403")), patch.object(downloader, "render_live_album", return_value=job / "live-photo.mp4") as render:
        result, _ = downloader._download_douyin_note_images("https://www.douyin.com/note/12345", job, 30)
    assert isinstance(result, Path)
    assert [item.is_live for item in render.call_args.args[0]] == [False, True]
    assert not (job / "douyin-cookies.txt").exists()


def test_xhs_static_post_uses_image_sender(tmp_path, monkeypatch):
    urls = ["https://sns-webpic.xhscdn.com/1", "https://sns-webpic.xhscdn.com/2"]
    monkeypatch.setattr(downloader, "_resolve_xiaohongshu_note", lambda *args: {"imageList": [{"urlDefault": url} for url in urls]})
    def asset(url, path, timeout):
        assert url in urls
        Image.new("RGB", (20, 30), "red").save(path, "JPEG")
    monkeypatch.setattr(downloader, "_download_xiaohongshu_asset", asset)
    with patch.object(downloader, "render_live_album") as render:
        images, logs = downloader._download_xiaohongshu_album("https://www.xiaohongshu.com/explore/674051740000000007027a15", tmp_path, 30)
    assert [path.name for path in images] == ["image-000.jpg", "image-001.jpg"]
    assert "小红书" in logs
    render.assert_not_called()


def test_xhs_short_link_recovery_keeps_mobile_share_parameters():
    response = Mock(url="https://www.xiaohongshu.com/404?source=note&noteId=674051740000000007027a15&xsec_token=demo&xsec_source=app_share&app_platform=ios&xhsshare=copylink")
    response.raise_for_status.return_value = None
    with patch.object(downloader.httpx, "get", return_value=response):
        result = downloader.resolve_xiaohongshu_share_url("https://xhslink.com/o/demo")
    from urllib.parse import parse_qs, urlparse
    query = parse_qs(urlparse(result).query)
    assert query["app_platform"] == ["ios"]
    assert query["xsec_token"] == ["demo"]
    assert query["xsec_source"] == ["app_share"]
    assert query["xhsshare"] == ["copylink"]


def test_douyin_live_download_has_no_site_credentials_or_external_config(tmp_path, monkeypatch):
    monkeypatch.setattr(downloader, "get_ytdlp_command", lambda: ["python", "-m", "yt_dlp"])
    target = tmp_path / "live-source-000.media"
    with patch.object(downloader, "_run_subprocess", return_value=SimpleNamespace(returncode=0)) as run:
        downloader._download_douyin_live_asset("https://api-play.amemv.com/aweme/v1/play/?video_id=demo", target, 12)
    arguments = run.call_args.args[0]
    assert "--ignore-config" in arguments
    assert "--cookies" not in arguments
    assert "--cookies-from-browser" not in arguments
    assert arguments[arguments.index("--output") + 1] == target.name
    assert run.call_args.kwargs["timeout"] == 12
    with patch.object(downloader, "_run_subprocess") as denied:
        with pytest.raises(downloader.DownloadError, match="不受信任"):
            downloader._download_douyin_live_asset("https://douyinvod.com.evil.test/a", target, 12)
        denied.assert_not_called()


def test_renderer_retries_bad_candidate_and_bounds_timeout(tmp_path):
    fetched = []
    def download(url, path, timeout):
        fetched.append((url, timeout))
        if url == "bad":
            raise OSError("network")
        path.write_bytes(b"video")
    def run(args, **kwargs):
        assert 0 < kwargs["timeout"] <= 20
        if args[0] == "ffprobe":
            return SimpleNamespace(returncode=0, stdout=json.dumps({"streams": [{"codec_type": "video", "width": 100, "height": 200, "duration": "0.5"}]}))
        (tmp_path / args[-1]).write_bytes(b"video")
        return SimpleNamespace(returncode=0)
    result = render_live_album([AlbumItem(("cover",), ("bad", "good"), True)], tmp_path, 20, download, run)
    assert result.name == "live-photo.mp4"
    assert [url for url, _ in fetched] == ["bad", "good"]
    assert all(0 < timeout <= 20 for _, timeout in fetched)
    with patch("app.live_photo.time.monotonic", side_effect=[0, 21]):
        with pytest.raises(LivePhotoError, match="超时"):
            render_live_album([AlbumItem((), ("good",), True)], tmp_path, 20, download, run)


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg unavailable")
def test_real_ffmpeg_keeps_motion_audio_and_mixed_album_order(tmp_path):
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    first, second, still = fixtures / "first.mov", fixtures / "second.mp4", fixtures / "still.png"
    common = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    subprocess.run([*common, "-f", "lavfi", "-i", "testsrc2=s=160x100:r=24", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "0.5", "-c:v", "libx265", "-x265-params", "log-level=error", "-c:a", "aac", str(first)], capture_output=True, check=True, timeout=20)
    subprocess.run([*common, "-f", "lavfi", "-i", "testsrc2=s=100x160:r=25", "-t", "0.4", "-c:v", "libx264", "-an", str(second)], capture_output=True, check=True, timeout=20)
    Image.new("RGB", (53, 71), "orange").save(still)
    job = tmp_path / "job"
    job.mkdir()
    def asset(url, output, timeout):
        shutil.copyfile({"first": first, "second": second, "still": still}[url], output)
    output = render_live_album([AlbumItem((), ("first",), True), AlbumItem(("still",)), AlbumItem((), ("second",), True)], job, 45, asset)
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(output)], capture_output=True, text=True, check=True, timeout=10)
    data = json.loads(probe.stdout)
    video = next(stream for stream in data["streams"] if stream["codec_type"] == "video")
    audio = next(stream for stream in data["streams"] if stream["codec_type"] == "audio")
    assert (video["codec_name"], video["pix_fmt"], video["width"], video["height"]) == ("h264", "yuv420p", 1280, 720)
    assert video["avg_frame_rate"] == "30/1"
    assert audio["codec_name"] == "aac"
    assert 3.8 <= float(data["format"]["duration"]) <= 4.2
    content = output.read_bytes()
    assert content.index(b"moov") < content.index(b"mdat")
    frames = subprocess.run([*common, "-i", str(output), "-an", "-vf", r"select=eq(n\,2)+eq(n\,9)", "-fps_mode", "vfr", "-f", "framemd5", "-"], capture_output=True, text=True, check=True, timeout=10)
    hashes = [line.rsplit(",", 1)[-1].strip() for line in frames.stdout.splitlines() if not line.startswith("#")]
    assert len(hashes) == 2 and hashes[0] != hashes[1]
    pcm = subprocess.run([*common, "-i", str(output), "-t", "0.2", "-vn", "-f", "s16le", "-"], capture_output=True, check=True, timeout=10)
    assert any(pcm.stdout)

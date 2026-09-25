import subprocess
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from PIL import Image

from app.downloader import (
    _douyin_cookie_arguments,
    _download_douyin_note_to_mp4,
    _resolve_douyin_video_media,
    _run_subprocess,
    _x_cookie_arguments,
    _xiaohongshu_cookie_arguments,
    DownloadError,
    NoVideoError,
    download_to_mp4,
    get_yutto_command,
    normalize_bilibili_url,
    normalize_douyin_url,
    normalize_video_url,
    normalize_x_url,
    normalize_xiaoheihe_url,
    normalize_xiaohongshu_url,
    resolve_douyin_share_url,
    resolve_xiaoheihe_video_url,
    resolve_xiaohongshu_share_url,
)


@pytest.mark.parametrize("url", [
    "https://www.bilibili.com/video/BV1xx411c7mD",
    "https://b23.tv/abcd1234",
    "http://m.bilibili.com/video/BV1xx411c7mD?p=2",
])
def test_accepts_supported_urls(url: str) -> None:
    assert normalize_bilibili_url(url) == url


@pytest.mark.parametrize("url", [
    "https://example.com/video/BV1xx411c7mD",
    "file:///etc/passwd",
    "https://bilibili.com.evil.example/video/1",
    "https://user:password@bilibili.com/video/1",
])
def test_rejects_unsupported_urls(url: str) -> None:
    with pytest.raises(ValueError):
        normalize_bilibili_url(url)


@pytest.mark.parametrize("url", [
    "https://x.com/example/status/1234567890",
    "https://www.x.com/example/status/1234567890?s=20",
    "https://twitter.com/i/status/9876543210/video/1",
])
def test_accepts_x_status_urls(url: str) -> None:
    assert normalize_x_url(url) == url
    assert normalize_video_url(url) == url


@pytest.mark.parametrize("url", [
    "https://x.com/example",
    "https://x.com/home",
    "https://x.com.evil.example/example/status/123",
    "https://user:password@x.com/example/status/123",
])
def test_rejects_non_status_x_urls(url: str) -> None:
    with pytest.raises(ValueError):
        normalize_x_url(url)


@pytest.mark.parametrize("url", [
    "https://v.douyin.com/iRNBho6u/",
    "https://www.douyin.com/video/7351234567890123456",
    "https://www.iesdouyin.com/share/video/7351234567890123456/?region=CN",
    "https://www.douyin.com/note/7677181147129952235",
    "https://www.douyin.com/?modal_id=7351234567890123456",
])
def test_accepts_douyin_video_urls(url: str) -> None:
    assert normalize_douyin_url(url) == url
    assert normalize_video_url(url) == url


@pytest.mark.parametrize("url", [
    "https://www.douyin.com/",
    "https://www.douyin.com/user/example",
    "https://v.douyin.com/",
    "https://v.douyin.com.evil.example/abc123",
])
def test_rejects_non_video_douyin_urls(url: str) -> None:
    with pytest.raises(ValueError):
        normalize_douyin_url(url)


@patch("app.downloader.httpx.get")
def test_resolves_douyin_short_link_to_note(get: Mock) -> None:
    response = Mock(url="https://www.douyin.com/note/7677181147129952235")
    response.raise_for_status = Mock()
    get.return_value = response
    assert resolve_douyin_share_url("https://v.douyin.com/t38tHvayn_Q/").endswith(
        "/note/7677181147129952235"
    )
    assert get.call_args.kwargs["follow_redirects"] is True


@pytest.mark.parametrize("url", [
    "https://xhslink.com/a/qsoHVeD0Liw1",
    "http://xhslink.com/m/4TTZOsqnDUp",
    "https://xhslink.cn/o/2wom7IQq1d2",
    "https://www.xiaohongshu.com/explore/674051740000000007027a15",
    "https://xiaohongshu.com/discovery/item/674051740000000007027a15?xsec_token=abc",
])
def test_accepts_xiaohongshu_note_urls(url: str) -> None:
    normalized = normalize_xiaohongshu_url(url)
    assert normalize_video_url(url) == normalized
    if "xiaohongshu.com" in url:
        assert normalized.startswith("https://www.xiaohongshu.com/")


@pytest.mark.parametrize("url", [
    "https://www.xiaohongshu.com/user/profile/abc",
    "https://xhslink.com/",
    "https://xhslink.cn/",
    "https://xhslink.com.evil.example/a/qsoHVeD0Liw1",
    "https://xhslink.cn.evil.example/o/2wom7IQq1d2",
    "https://user:password@www.xiaohongshu.com/explore/674051740000000007027a15",
])
def test_rejects_non_note_xiaohongshu_urls(url: str) -> None:
    with pytest.raises(ValueError):
        normalize_xiaohongshu_url(url)


@patch("app.downloader.httpx.get")
def test_resolves_xiaohongshu_short_link_safely(get: Mock) -> None:
    response = Mock(
        url="https://www.xiaohongshu.com/discovery/item/674051740000000007027a15?xsec_token=abc"
    )
    response.raise_for_status = Mock()
    get.return_value = response

    result = resolve_xiaohongshu_share_url("https://xhslink.com/a/qsoHVeD0Liw1")

    assert result.startswith("https://www.xiaohongshu.com/discovery/item/674051740000000007027a15")
    assert get.call_args.kwargs["follow_redirects"] is True


@patch("app.downloader.httpx.get")
def test_recovers_xiaohongshu_note_id_from_404_redirect(get: Mock) -> None:
    response = Mock(
        url=(
            "https://www.xiaohongshu.com/404?source=note&"
            "noteId=6956513d000000001e022eda&xsec_token=token&type=video"
        )
    )
    response.raise_for_status = Mock()
    get.return_value = response

    result = resolve_xiaohongshu_share_url("https://xhslink.com/o/2AekhokTCiX")

    assert result.startswith("https://www.xiaohongshu.com/explore/6956513d000000001e022eda?")
    assert "xsec_token=token" in result


@pytest.mark.parametrize("url", [
    "https://api.xiaoheihe.cn/v3/bbs/app/api/web/share?link_id=5a28725c3264",
    "https://www.xiaoheihe.cn/app/bbs/link/5a28725c3264?h_camp=link",
])
def test_accepts_xiaoheihe_share_urls(url: str) -> None:
    assert normalize_xiaoheihe_url(url) == url
    assert normalize_video_url(url) == url


@pytest.mark.parametrize("url", [
    "https://api.xiaoheihe.cn/v3/bbs/app/api/web/share",
    "https://www.xiaoheihe.cn/app/bbs/link/",
    "https://xiaoheihe.cn.evil.example/app/bbs/link/5a28725c3264",
])
def test_rejects_invalid_xiaoheihe_urls(url: str) -> None:
    with pytest.raises(ValueError):
        normalize_xiaoheihe_url(url)


@patch("app.downloader.httpx.get")
def test_resolves_xiaoheihe_video_safely(get: Mock) -> None:
    get.return_value = Mock(
        json=lambda: {
            "status": "ok",
            "result": {"link": {"video_url": "https://videoheybox.max-c.com/a/video.mp4?auth=1"}},
        },
        raise_for_status=lambda: None,
    )
    url = "https://api.xiaoheihe.cn/v3/bbs/app/api/web/share?link_id=5a28725c3264"
    assert resolve_xiaoheihe_video_url(url) == "https://videoheybox.max-c.com/a/video.mp4?auth=1"
    assert get.call_args.kwargs["follow_redirects"] is False


@patch("app.downloader.httpx.get")
def test_rejects_untrusted_xiaoheihe_media_host(get: Mock) -> None:
    get.return_value = Mock(
        json=lambda: {
            "status": "ok",
            "result": {"link": {"video_url": "https://evil.example/video.mp4"}},
        },
        raise_for_status=lambda: None,
    )
    with pytest.raises(DownloadError, match="不受信任"):
        resolve_xiaoheihe_video_url(
            "https://www.xiaoheihe.cn/app/bbs/link/5a28725c3264"
        )


@patch("app.downloader.httpx.get")
def test_xiaoheihe_text_post_is_not_a_conversion_failure(get: Mock) -> None:
    get.return_value = Mock(
        json=lambda: {
            "status": "ok",
            "result": {"link": {"has_video": 0, "title": "文字帖子"}},
        },
        raise_for_status=lambda: None,
    )
    with pytest.raises(NoVideoError, match="没有视频"):
        resolve_xiaoheihe_video_url(
            "https://www.xiaoheihe.cn/app/bbs/link/5a28725c3264"
        )


@patch("app.downloader.ensure_runtime", return_value=["python", "-m", "yutto"])
@patch("app.downloader._run_subprocess")
def test_download_invokes_yutto_safely(run: Mock, _: Mock, tmp_path: Path) -> None:
    run.return_value = Mock(returncode=0, stdout="ok", stderr="")
    job_dir = tmp_path / "job"

    def create_output(*args: object, **kwargs: object) -> Mock:
        (job_dir / "video.mp4").write_bytes(b"video")
        return Mock(returncode=0, stdout="ok", stderr="")

    run.side_effect = create_output
    output, _logs = download_to_mp4("https://b23.tv/abc;whoami", job_dir, 30)
    args = run.call_args.args[0]
    assert args[-1] == "https://b23.tv/abc;whoami"
    assert args[:6] == ["python", "-m", "yutto", "download", "--output-format", "mp4"]
    assert run.call_args.kwargs["cwd"] == job_dir
    assert run.call_args.kwargs["env"]["PYTHONIOENCODING"] == "utf-8"
    assert run.call_args.kwargs["env"]["PYTHONUTF8"] == "1"
    assert "shell" not in run.call_args.kwargs
    assert output.name == "video.mp4"


@patch("app.downloader.ensure_runtime", return_value=["python", "-m", "yutto"])
@patch("app.downloader._run_subprocess")
def test_download_reports_yutto_failure(run: Mock, _: Mock, tmp_path: Path) -> None:
    run.return_value = Mock(returncode=2, stdout="", stderr="download failed")
    with pytest.raises(DownloadError, match="download failed"):
        download_to_mp4("https://b23.tv/abc", tmp_path / "job", 30)


@patch.dict("os.environ", {}, clear=True)
@patch("app.downloader.ensure_runtime", return_value=["python", "-m", "yt_dlp"])
@patch("app.downloader._run_subprocess")
def test_download_invokes_ytdlp_for_x(run: Mock, _: Mock, tmp_path: Path) -> None:
    job_dir = tmp_path / "x-job"

    def create_output(*args: object, **kwargs: object) -> Mock:
        (job_dir / "1234567890.mp4").write_bytes(b"video")
        return Mock(returncode=0, stdout="ok", stderr="")

    run.side_effect = create_output
    url = "https://x.com/example/status/1234567890"
    output, _logs = download_to_mp4(url, job_dir, 30)
    args = run.call_args.args[0]
    assert args[:3] == ["python", "-m", "yt_dlp"]
    assert "twitter:api=syndication" not in args
    assert "--extractor-retries" in args
    assert args[-1] == url
    assert run.call_args.kwargs["cwd"] == job_dir
    assert "shell" not in run.call_args.kwargs
    assert output.name == "1234567890.mp4"


@patch.dict("os.environ", {}, clear=True)
@patch("app.downloader.ensure_runtime", return_value=["python", "-m", "yt_dlp"])
@patch("app.downloader._run_subprocess")
def test_x_download_falls_back_to_syndication(run: Mock, _: Mock, tmp_path: Path) -> None:
    job_dir = tmp_path / "x-fallback"

    def run_attempt(arguments: list[str], **kwargs: object) -> Mock:
        if "twitter:api=syndication" not in arguments:
            return Mock(returncode=1, stdout="", stderr="default failed")
        (job_dir / "1234567890.mp4").write_bytes(b"video")
        return Mock(returncode=0, stdout="fallback ok", stderr="")

    run.side_effect = run_attempt
    output, logs = download_to_mp4(
        "https://x.com/example/status/1234567890/video/1",
        job_dir,
        30,
    )

    assert run.call_count == 2
    assert "twitter:api=syndication" not in run.call_args_list[0].args[0]
    assert "twitter:api=syndication" in run.call_args_list[1].args[0]
    assert run.call_args_list[1].args[0][-1].endswith("/video/1")
    assert output.name == "1234567890.mp4"
    assert logs == "fallback ok"


@patch.dict("os.environ", {}, clear=True)
@patch("app.downloader.ensure_runtime", return_value=["python", "-m", "yt_dlp"])
@patch("app.downloader._run_subprocess")
def test_download_invokes_ytdlp_for_douyin(run: Mock, _: Mock, tmp_path: Path) -> None:
    job_dir = tmp_path / "douyin-job"

    def create_output(*args: object, **kwargs: object) -> Mock:
        (job_dir / "7351234567890123456.mp4").write_bytes(b"video")
        return Mock(returncode=0, stdout="ok", stderr="")

    run.side_effect = create_output
    url = "https://v.douyin.com/iRNBho6u/"
    output, _logs = download_to_mp4(url, job_dir, 30)
    args = run.call_args.args[0]

    assert args[:3] == ["python", "-m", "yt_dlp"]
    assert "https://www.douyin.com/" in args
    assert "--extractor-retries" in args
    assert args[-1] == url
    assert output.name == "7351234567890123456.mp4"


@patch("app.downloader._find_douyin_cookie_file", return_value=None)
@patch("app.downloader.httpx.get")
def test_douyin_detail_preserves_lowercase_header_and_selects_h264(
    get: Mock, _cookie: Mock, tmp_path: Path
) -> None:
    get.return_value.json.return_value = {"aweme_detail": {
        "aweme_id": "12345", "video": {
            "play_addr_h264": {"url_list": ["https://v1.douyinvod.com/h264.mp4"]},
            "play_addr": {"url_list": ["https://v1.douyinvod.com/h265.mp4"]},
        },
    }}
    assert _resolve_douyin_video_media(
        "https://www.douyin.com/?modal_id=12345", tmp_path, 30
    ) == "https://v1.douyinvod.com/h264.mp4"
    assert get.call_args.kwargs["headers"]["x-tt-argus"] == "1"
    assert get.call_args.kwargs["params"] == {"aweme_id": "12345"}


@pytest.mark.parametrize("detail", [
    {"aweme_id": "99999", "video": {"play_addr": {"url_list": ["https://v1.douyinvod.com/a"]}}},
    {"aweme_id": "12345", "video": {"play_addr": {"url_list": ["https://douyinvod.com.evil.test/a"]}}},
    {"aweme_id": "12345", "video": {"play_addr": {"url_list": ["http://127.0.0.1/a"]}}},
])
@patch("app.downloader._find_douyin_cookie_file", return_value=None)
@patch("app.downloader.httpx.get")
def test_douyin_detail_rejects_wrong_item_and_untrusted_media(
    get: Mock, _cookie: Mock, detail: dict, tmp_path: Path
) -> None:
    get.return_value.json.return_value = {"aweme_detail": detail}
    with pytest.raises(DownloadError):
        _resolve_douyin_video_media("https://www.douyin.com/video/12345", tmp_path, 30)


@patch("app.downloader._douyin_cookie_arguments", return_value=[])
@patch("app.downloader._resolve_douyin_video_media", return_value="https://v1.douyinvod.com/a.mp4")
@patch("app.downloader.ensure_runtime", return_value=["python", "-m", "yt_dlp"])
@patch("app.downloader._run_subprocess")
@pytest.mark.parametrize("fallback_ok", [True, False])
def test_douyin_403_retries_media_once(
    run: Mock, _runtime: Mock, resolve: Mock, _cookies: Mock,
    fallback_ok: bool, tmp_path: Path,
) -> None:
    job_dir = tmp_path / "job"
    def attempt(arguments, **kwargs):
        if arguments[-1] == "https://v1.douyinvod.com/a.mp4" and fallback_ok:
            (job_dir / "result.mp4").write_bytes(b"video")
            return Mock(returncode=0, stdout="downloaded", stderr="")
        return Mock(returncode=1, stdout="", stderr="HTTP Error 403: Fresh cookies are needed")
    run.side_effect = attempt
    if fallback_ok:
        output, _ = download_to_mp4("https://www.douyin.com/video/12345", job_dir, 30)
        assert output.read_bytes() == b"video"
    else:
        with pytest.raises(DownloadError):
            download_to_mp4("https://www.douyin.com/video/12345", job_dir, 30)
    assert run.call_count == 2
    resolve.assert_called_once()


@patch("app.downloader._download_douyin_note_to_mp4")
@patch("app.downloader.ensure_runtime", return_value=["python", "-m", "yt_dlp"])
def test_douyin_note_uses_image_post_converter(
    _runtime: Mock, convert: Mock, tmp_path: Path
) -> None:
    job_dir = tmp_path / "douyin-note-job"
    expected = job_dir / "7677181147129952235.mp4"
    convert.return_value = (expected, "ok")
    url = "https://www.douyin.com/note/7677181147129952235"

    output, logs = download_to_mp4(url, job_dir, 30)

    convert.assert_called_once_with(url, job_dir, 30)
    assert output == expected
    assert logs == "ok"


@patch("app.downloader._douyin_cookie_file", return_value=Path("cookies.txt"))
@patch("app.downloader._douyin_f2_python", return_value=Path("f2-python.exe"))
@patch("app.downloader._download_douyin_asset")
@patch("app.downloader._run_subprocess")
def test_douyin_note_builds_mp4_from_downloaded_images(
    run: Mock,
    download_asset: Mock,
    _f2_python: Mock,
    _cookie_file: Mock,
    tmp_path: Path,
) -> None:
    job_dir = tmp_path / "note"
    job_dir.mkdir()
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text("cookie", encoding="utf-8")
    _cookie_file.return_value = cookie_file

    def fake_process(arguments: list[str], **kwargs: object) -> Mock:
        cwd = Path(kwargs["cwd"])
        if arguments[0] == "f2-python.exe":
            (cwd / "note.json").write_text(
                '{"aweme_id":"7677181147129952235","images":'
                '["https://p3-sign.douyinpic.com/a","https://p9-sign.douyinpic.com/b"],'
                '"music_url":null}',
                encoding="utf-8",
            )
        else:
            (cwd / arguments[-1]).write_bytes(b"mp4")
        return Mock(returncode=0, stdout="ok", stderr="")

    def fake_asset(_url: str, output: Path, _timeout: int) -> None:
        Image.new("RGB", (320, 180), "orange").save(output, "JPEG")

    run.side_effect = fake_process
    download_asset.side_effect = fake_asset

    output, _logs = _download_douyin_note_to_mp4(
        "https://www.douyin.com/note/7677181147129952235", job_dir, 30
    )

    assert output.name == "7677181147129952235.mp4"
    assert not (job_dir / "douyin-cookies.txt").exists()
    assert (job_dir / "slide-000.jpg").is_file()
    assert Image.open(job_dir / "slide-000.jpg").size == (720, 1280)
    ffmpeg_arguments = run.call_args_list[-1].args[0]
    assert ffmpeg_arguments[0] == "ffmpeg"
    assert "libx264" in ffmpeg_arguments


@patch.dict("os.environ", {}, clear=True)
@patch("app.downloader._download_douyin_note_to_mp4")
@patch(
    "app.downloader.resolve_douyin_share_url",
    return_value="https://v.douyin.com/t38tHvayn_Q/",
)
@patch("app.downloader.ensure_runtime", return_value=["python", "-m", "yt_dlp"])
@patch("app.downloader._run_subprocess")
def test_failed_ytdlp_can_fall_back_when_log_reveals_note_url(
    run: Mock, _runtime: Mock, _resolve: Mock, convert: Mock, tmp_path: Path
) -> None:
    run.return_value = Mock(
        returncode=1,
        stdout="",
        stderr="Unsupported URL: https://www.douyin.com/note/7677181147129952235",
    )
    expected = tmp_path / "job" / "note.mp4"
    convert.return_value = (expected, "converted")

    output, logs = download_to_mp4(
        "https://v.douyin.com/t38tHvayn_Q/", tmp_path / "job", 30
    )

    convert.assert_called_once_with(
        "https://www.douyin.com/note/7677181147129952235", tmp_path / "job", 30
    )
    assert output == expected
    assert logs == "converted"


@patch.dict("os.environ", {"DOUYIN_COOKIES_FROM_BROWSER": "edge"}, clear=True)
@patch("app.downloader.ensure_runtime", return_value=["python", "-m", "yt_dlp"])
@patch("app.downloader._run_subprocess")
def test_douyin_can_use_explicit_browser_cookies(run: Mock, _: Mock, tmp_path: Path) -> None:
    job_dir = tmp_path / "douyin-cookie-job"

    def create_output(*args: object, **kwargs: object) -> Mock:
        (job_dir / "video.mp4").write_bytes(b"video")
        return Mock(returncode=0, stdout="ok", stderr="")

    run.side_effect = create_output
    download_to_mp4("https://v.douyin.com/iRNBho6u/", job_dir, 30)

    arguments = run.call_args.args[0]
    index = arguments.index("--cookies-from-browser")
    assert arguments[index + 1] == "edge"


def test_douyin_cookie_file_is_copied_before_ytdlp_can_rewrite_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "exported-cookies.txt"
    original = (
        "# Netscape HTTP Cookie File\n"
        ".douyin.com\tTRUE\t/\tTRUE\t1999999999\tsessionid\ttest-value\n"
    )
    source.write_text(original, encoding="utf-8")
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    monkeypatch.setenv("DOUYIN_COOKIES_FILE", str(source))
    monkeypatch.delenv("DOUYIN_COOKIES_FROM_BROWSER", raising=False)

    arguments = _douyin_cookie_arguments(job_dir)
    runtime_copy = Path(arguments[1])
    runtime_copy.write_text("yt-dlp rewrote this file", encoding="utf-8")

    assert runtime_copy.parent == job_dir
    assert source.read_text(encoding="utf-8") == original


def test_douyin_cookie_file_is_auto_detected_from_windows_public(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    public_dir = tmp_path / "Public"
    public_dir.mkdir()
    source = public_dir / "www.douyin.com_cookies.txt"
    source.write_text(
        "# Netscape HTTP Cookie File\n"
        ".douyin.com\tTRUE\t/\tTRUE\t1999999999\tsessionid\ttest-value\n",
        encoding="utf-8",
    )
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    monkeypatch.delenv("DOUYIN_COOKIES_FILE", raising=False)
    monkeypatch.delenv("DOUYIN_COOKIES_FROM_BROWSER", raising=False)
    monkeypatch.setenv("PUBLIC", str(public_dir))

    arguments = _douyin_cookie_arguments(job_dir)

    assert arguments[0] == "--cookies"
    assert Path(arguments[1]).parent == job_dir
    assert Path(arguments[1]).read_text(encoding="utf-8").startswith(
        "# Netscape HTTP Cookie File"
    )


def test_douyin_rejects_cookie_file_without_cookie_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "empty-cookies.txt"
    source.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    monkeypatch.setenv("DOUYIN_COOKIES_FILE", str(source))

    with pytest.raises(DownloadError, match="没有有效 Cookie"):
        _douyin_cookie_arguments(tmp_path)


def test_xiaohongshu_cookie_file_is_copied_before_ytdlp_can_rewrite_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "exported-xhs-cookies.txt"
    source.write_text("original cookie contents", encoding="utf-8")
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    monkeypatch.setenv("XIAOHONGSHU_COOKIES_FILE", str(source))
    monkeypatch.delenv("XIAOHONGSHU_COOKIES_FROM_BROWSER", raising=False)

    arguments = _xiaohongshu_cookie_arguments(job_dir)
    runtime_copy = Path(arguments[1])
    runtime_copy.write_text("yt-dlp rewrote this file", encoding="utf-8")

    assert runtime_copy.parent == job_dir
    assert source.read_text(encoding="utf-8") == "original cookie contents"


def test_x_cookie_file_is_copied_and_removed_after_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "exported-x-cookies.txt"
    original = "# Netscape HTTP Cookie File\n.x.com\tTRUE\t/\tTRUE\t1999999999\tct0\ttest-value\n"
    source.write_text(original, encoding="utf-8")
    job_dir = tmp_path / "job"
    monkeypatch.setenv("X_COOKIES_FILE", str(source))
    monkeypatch.delenv("X_COOKIES_FROM_BROWSER", raising=False)

    monkeypatch.setattr("app.downloader.ensure_runtime", lambda _platform: ["python", "-m", "yt_dlp"])

    def fake_run(arguments: list[str], **_kwargs: object) -> Mock:
        runtime_copy = Path(arguments[arguments.index("--cookies") + 1])
        assert runtime_copy.parent == job_dir
        assert runtime_copy.read_text(encoding="utf-8") == original
        runtime_copy.write_text("yt-dlp updated this task copy", encoding="utf-8")
        (job_dir / "1234567890.mp4").write_bytes(b"video")
        return Mock(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr("app.downloader._run_subprocess", fake_run)
    download_to_mp4("https://x.com/example/status/1234567890", job_dir, 30)

    assert not (job_dir / "x-cookies.txt").exists()
    assert source.read_text(encoding="utf-8") == original


def test_run_subprocess_kills_windows_process_tree_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = Mock(pid=4321)
    process.poll.return_value = None
    process.communicate.side_effect = [
        subprocess.TimeoutExpired(["yt-dlp"], 1),
        ("partial stdout", "partial stderr"),
    ]
    killer = Mock()
    monkeypatch.setattr("app.downloader.os.name", "nt")
    monkeypatch.setattr("app.downloader.subprocess.Popen", Mock(return_value=process))
    monkeypatch.setattr("app.downloader.subprocess.run", killer)

    with pytest.raises(subprocess.TimeoutExpired):
        _run_subprocess(["yt-dlp", "url"], timeout=1, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    killer.assert_called_once()
    assert killer.call_args.args[0] == ["taskkill", "/PID", "4321", "/T", "/F"]
    process.kill.assert_called_once_with()
    assert process.communicate.call_count == 2


@patch.dict("os.environ", {}, clear=True)
@patch(
    "app.downloader.resolve_xiaohongshu_share_url",
    return_value="https://www.xiaohongshu.com/explore/674051740000000007027a15",
)
@patch("app.downloader.ensure_runtime", return_value=["python", "-m", "yt_dlp"])
@patch("app.downloader._run_subprocess")
def test_download_invokes_ytdlp_for_xiaohongshu(
    run: Mock, _runtime: Mock, resolve: Mock, tmp_path: Path
) -> None:
    job_dir = tmp_path / "xiaohongshu-job"

    def create_output(*args: object, **kwargs: object) -> Mock:
        (job_dir / "674051740000000007027a15.mp4").write_bytes(b"video")
        return Mock(returncode=0, stdout="ok", stderr="")

    run.side_effect = create_output
    source = "https://xhslink.com/a/qsoHVeD0Liw1"
    output, _logs = download_to_mp4(source, job_dir, 30)
    args = run.call_args.args[0]

    resolve.assert_called_once_with(source, 30)
    assert "--referer" in args
    assert "https://www.xiaohongshu.com/" in args
    assert args[-1].startswith("https://www.xiaohongshu.com/explore/")
    assert output.name == "674051740000000007027a15.mp4"


@patch.dict("os.environ", {}, clear=True)
@patch("app.downloader.ensure_runtime", return_value=["python", "-m", "yt_dlp"])
@patch("app.downloader._run_subprocess")
def test_xiaohongshu_image_note_is_not_a_conversion_failure(
    run: Mock, _runtime: Mock, tmp_path: Path
) -> None:
    run.return_value = Mock(
        returncode=1,
        stdout="",
        stderr="ERROR: [XiaoHongShu] note: No video formats found!",
    )
    with pytest.raises(NoVideoError, match="没有视频"):
        download_to_mp4(
            "https://www.xiaohongshu.com/explore/674051740000000007027a15",
            tmp_path / "xiaohongshu-image-note",
            30,
        )


@patch("app.downloader.resolve_xiaoheihe_video_url", return_value="https://videoheybox.max-c.com/a/video.mp4?auth=1")
@patch("app.downloader.ensure_runtime", return_value=["python", "-m", "yt_dlp"])
@patch("app.downloader._run_subprocess")
def test_download_invokes_ytdlp_for_xiaoheihe(
    run: Mock, _: Mock, resolve: Mock, tmp_path: Path
) -> None:
    job_dir = tmp_path / "xiaoheihe-job"

    def create_output(*args: object, **kwargs: object) -> Mock:
        (job_dir / "video.mp4").write_bytes(b"video")
        return Mock(returncode=0, stdout="ok", stderr="")

    run.side_effect = create_output
    source = "https://api.xiaoheihe.cn/v3/bbs/app/api/web/share?link_id=5a28725c3264"
    output, _logs = download_to_mp4(source, job_dir, 30)
    args = run.call_args.args[0]
    resolve.assert_called_once_with(source, 30)
    assert "--referer" in args
    assert args[-1].startswith("https://videoheybox.max-c.com/")
    assert output.name == "video.mp4"


@patch.dict("os.environ", {}, clear=True)
def test_uses_yutto_from_current_python_environment() -> None:
    command = get_yutto_command()
    assert command[1:] == ["-m", "yutto"]

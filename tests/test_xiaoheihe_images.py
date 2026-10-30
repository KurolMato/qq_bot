from contextlib import contextmanager
import json
from unittest.mock import Mock, patch

import httpx
import pytest
from PIL import Image

from app import downloader as d


POST = "https://www.xiaoheihe.cn/app/bbs/link/5a28725c3264"


def detail(link):
    return httpx.Response(200, json={"status": "ok", "result": {"link": link}}, request=httpx.Request("GET", POST))


def test_post_images_download_in_original_order_without_video_runtime(tmp_path, monkeypatch):
    urls = [f"https://imgheybox.max-c.com/{i}.jpg" for i in range(6)]
    monkeypatch.setattr(d.httpx, "get", Mock(return_value=detail({"imgs": urls, "title": " 小黑盒图文标题 &amp; 图片 "})))
    requested = []
    def image(url, output, timeout):
        requested.append(url)
        assert 0 < timeout <= 30
        Image.new("RGB", (100 + len(requested), 50), "red").save(output, "JPEG")
    monkeypatch.setattr(d, "_download_xiaoheihe_image", image)
    with patch.object(d, "ensure_runtime") as runtime, patch.object(d, "_run_subprocess") as process:
        images, logs = d.download_to_mp4(POST, tmp_path / "job", 30)
    assert requested == urls
    assert images.title == "小黑盒图文标题 & 图片"
    assert [image.name for image in images] == [f"image-{i:03d}.jpg" for i in range(6)]
    assert "6 张小黑盒图片" in logs
    runtime.assert_not_called()
    process.assert_not_called()


def test_extracts_owned_images_and_rich_text_without_avatars_or_comments():
    assert d._xiaoheihe_image_urls({
        "imgs": ["https://img.max-c.com/a?x=1&amp;y=2", {"url": "https://img.max-c.com/thumb", "original_url": "https://img.max-c.com/b"}],
        "user": {"avatar": "avatar"}, "comments": [{"imgs": ["comment"]}],
    }) == ["https://img.max-c.com/a?x=1&y=2", "https://img.max-c.com/b"]
    assert d._xiaoheihe_image_urls({"text": '<p>文字<img src="//img.max-c.com/first"></p><img data-original="https://img.max-c.com/second" src="thumbnail"><img src="//img.max-c.com/first">'}) == ["//img.max-c.com/first", "https://img.max-c.com/second"]


@pytest.mark.parametrize("encode_json", [False, True])
def test_real_post_shape_extracts_seven_structured_body_images(encode_json):
    urls = [f"https://imgheybox.max-c.com/{i}.jpg" for i in range(7)]
    blocks = [{"type": "text", "text": "神了"}, *[
        {"type": "img", "url": url, "live_url": "", "width": 100, "height": 200}
        for url in urls
    ]]
    body = json.dumps(blocks, ensure_ascii=False) if encode_json else blocks
    assert d._xiaoheihe_image_urls({"has_video": 0, "text": body, "description": "摘要"}) == urls


def test_structured_body_keeps_mixed_image_and_html_order_without_video_covers():
    blocks = [
        {"type": "html", "text": '<p><img src="https://img.max-c.com/first"></p>'},
        {"type": "img", "url": "https://img.max-c.com/second"},
        {"type": "video", "url": "video", "cover": "cover"},
        {"type": "text", "text": '<img src="https://img.max-c.com/last">'},
    ]
    assert d._xiaoheihe_image_urls({"text": json.dumps(blocks)}) == [
        "https://img.max-c.com/first", "https://img.max-c.com/second", "https://img.max-c.com/last",
    ]


def test_video_post_does_not_send_its_cover_as_album(monkeypatch):
    url = "https://videoheybox.max-c.com/a.mp4"
    monkeypatch.setattr(d.httpx, "get", Mock(return_value=detail({"video_url": url, "imgs": ["https://img.max-c.com/cover.jpg"]})))
    assert d.resolve_xiaoheihe_video_url(POST) == url


@pytest.mark.parametrize("link", [{}, {"text": "文字帖子"}, {"imgs": []}])
def test_text_only_post_remains_ignored(link, monkeypatch):
    monkeypatch.setattr(d.httpx, "get", Mock(return_value=detail(link)))
    with pytest.raises(d.NoVideoError):
        d.resolve_xiaoheihe_video_url(POST)


@pytest.mark.parametrize("url", ["https://max-c.com.evil.test/a", "https://127.0.0.1/a", "https://user:password@img.max-c.com/a", "https://img.max-c.com:9999/a"])
def test_rejects_untrusted_image_before_request(url, tmp_path):
    with patch.object(d.httpx, "stream") as stream:
        with pytest.raises(d.DownloadError, match="不受信任"):
            d._download_xiaoheihe_image(url, tmp_path / "image", 10)
    stream.assert_not_called()


def test_image_redirect_is_checked_before_connection(tmp_path, monkeypatch):
    calls = []
    @contextmanager
    def stream(method, url, **kwargs):
        calls.append(url)
        assert kwargs["follow_redirects"] is False
        yield httpx.Response(302, headers={"location": "https://evil.test/secret"}, request=httpx.Request("GET", url))
    monkeypatch.setattr(d.httpx, "stream", stream)
    with pytest.raises(d.DownloadError, match="不受信任"):
        d._download_xiaoheihe_image("http://img.max-c.com/a", tmp_path / "image", 10)
    assert calls == ["https://img.max-c.com/a"]


def test_image_download_uses_shared_album_budget_and_keeps_gif(tmp_path, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(d.time, "monotonic", lambda: clock[0])
    def download(url, output, timeout):
        assert timeout == 10
        Image.new("RGB", (10, 10), "red").save(output, "GIF")
        clock[0] += 11
    with patch.object(d, "_download_xiaoheihe_image", side_effect=download) as asset:
        images, logs = d._download_xiaoheihe_images(["first", "second"], tmp_path, 10)
    assert [image.suffix for image in images] == [".gif"]
    assert "第 2 张图片失败" in logs
    assert asset.call_count == 1


def test_all_failed_images_are_a_failure_without_sensitive_urls(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "_download_xiaoheihe_image", Mock(side_effect=httpx.ConnectError("private token")))
    with pytest.raises(d.DownloadError, match="小黑盒图文图片均下载失败") as error:
        d._download_xiaoheihe_images(["first", "second"], tmp_path, 10)
    assert "private token" not in str(error.value)


@pytest.mark.parametrize("state, expected", [("show_captcha", "要求验证码"), ("login", "要求登录"), ("relogin", "要求登录"), ("lack_token", "要求登录")])
def test_platform_verification_is_reported_without_retry_or_sensitive_data(state, expected, monkeypatch):
    response = httpx.Response(200, json={"status": state, "msg": "private server data", "result": {"ticket": "secret"}}, request=httpx.Request("GET", POST))
    monkeypatch.delenv("XIAOHEIHE_COOKIES_FILE", raising=False)
    get = Mock(return_value=response)
    monkeypatch.setattr(d.httpx, "get", get)
    with pytest.raises(d.DownloadError, match=expected) as error:
        d.resolve_xiaoheihe_video_url(POST)
    assert "private server data" not in str(error.value)
    assert "secret" not in str(error.value)
    assert "XIAOHEIHE_COOKIES_FILE" in str(error.value)
    assert get.call_count == 1


def test_cookie_is_scoped_to_detail_and_original_is_unchanged(tmp_path, monkeypatch):
    cookie = tmp_path / "cookies.txt"
    content = "# Netscape HTTP Cookie File\n.xiaoheihe.cn\tTRUE\t/\tTRUE\t1999999999\tuser_pkey\tprivate\n.xiaoheihe.cn\tTRUE\t/\tTRUE\t1\texpired\told\n.evil.test\tTRUE\t/\tTRUE\t1999999999\tother\twrong\n"
    cookie.write_text(content, encoding="utf-8")
    monkeypatch.setenv("XIAOHEIHE_COOKIES_FILE", str(cookie))
    get = Mock(return_value=detail({"imgs": ["https://img.max-c.com/a"]}))
    monkeypatch.setattr(d.httpx, "get", get)
    @contextmanager
    def stream(method, url, **kwargs):
        assert "cookies" not in kwargs
        assert "Cookie" not in kwargs.get("headers", {})
        yield httpx.Response(200, content=b"image", request=httpx.Request("GET", url))
    monkeypatch.setattr(d.httpx, "stream", stream)
    with pytest.raises(d._XiaoheiheAlbum):
        d.resolve_xiaoheihe_video_url(POST)
    cookies = get.call_args.kwargs["cookies"]
    assert [(c.name, c.domain) for c in cookies] == [("user_pkey", ".xiaoheihe.cn")]
    d._download_xiaoheihe_image("https://img.max-c.com/a", tmp_path / "img", 10)
    assert cookie.read_text(encoding="utf-8") == content


@pytest.mark.parametrize("content", [None, "not a cookie export", "# Netscape HTTP Cookie File\n.evil.test\tTRUE\t/\tTRUE\t1999999999\tuser\tsecret\n"])
def test_invalid_cookie_stops_with_actionable_error(tmp_path, monkeypatch, content):
    cookie = tmp_path / "cookies.txt"
    if content is not None:
        cookie.write_text(content, encoding="utf-8")
    monkeypatch.setenv("XIAOHEIHE_COOKIES_FILE", str(cookie))
    with patch.object(d.httpx, "get") as get:
        with pytest.raises(d.DownloadError, match="Cookie|COOKIES_FILE"):
            d.resolve_xiaoheihe_video_url(POST)
    get.assert_not_called()

from contextlib import contextmanager
import json
from unittest.mock import Mock, patch

import httpx
import pytest
from PIL import Image

from app import bilibili_images as b, downloader as d


ID = "917675369769730096"
URL = "https://www.bilibili.com/opus/" + ID


def content(urls):
    return {"id_str": ID, "modules": [{"module_author": {"face": "avatar"}}, {"module_type": "MODULE_TYPE_CONTENT", "module_content": {"paragraphs": [
        {"para_type": 1, "text": {"nodes": [{"rich": {"emoji": {"icon_url": "emoji"}}}]}},
        *[{"para_type": 2, "pic": {"pics": [{"url": url, "live_url": ""}]}} for url in urls],
        {"para_type": 6, "link_card": {"cover": "recommendation"}},
    ]}}]}


def page(item):
    return '<script>window.__INITIAL_STATE__=' + json.dumps({"detail": item}) + ';(function(){})()</script>'


@pytest.mark.parametrize("url,expected", [
    (URL, ("opus", ID)),
    ("https://t.bilibili.com/"+ID, ("opus", ID)),
    ("https://m.bilibili.com/dynamic/"+ID, ("opus", ID)),
    ("https://www.bilibili.com/read/cv123", ("article", "123")),
    ("https://www.bilibili.com/read/mobile?id=123", ("article", "123")),
    ("https://www.bilibili.com/video/BV123", None),
])
def test_recognizes_image_post_formats(url, expected):
    assert b.post_id(url) == expected


def test_public_page_downloads_seven_original_images_without_video_tools(tmp_path, monkeypatch):
    urls = [f"http://i0.hdslb.com/bfs/new_dyn/{i}.png" for i in range(7)]
    item = content(urls)
    item["modules"].insert(0, {"module_type": "MODULE_TYPE_TITLE", "module_title": {"text": " B站图文标题 "}})
    get = Mock(return_value=httpx.Response(200, text=page(item), request=httpx.Request("GET", URL)))
    monkeypatch.setattr(b.httpx, "get", get)
    seen = []
    def asset(url, output, timeout):
        seen.append(url)
        assert 0 < timeout <= 30
        Image.new("RGB", (40+len(seen), 70), "red").save(output, "PNG")
    monkeypatch.setattr(b, "download_image", asset)
    with patch.object(d, "ensure_runtime") as runtime:
        images, logs = d.download_to_mp4(URL, tmp_path / "job", 30)
    assert seen == urls
    assert len(images) == 7
    assert images.title == "B站图文标题"
    assert [image.name for image in images] == [f"image-{i:03d}.png" for i in range(7)]
    assert "7 张B 站图片" in logs
    assert get.call_count == 1  # SSR success does not need the protected API.
    runtime.assert_not_called()


@pytest.mark.parametrize("key,field", [("draw", "src"), ("opus", "url")])
def test_dynamic_api_pictures_exclude_portraits_and_covers(key, field):
    major = {key: {"items" if key == "draw" else "pics": [{field: "first"}, {field: "last"}]}, "archive": {"cover": "cover"}}
    item = {"id_str": ID, "modules": {"module_author": {"face": "avatar"}, "module_dynamic": {"major": major}}}
    assert b.content_images(item, ID).images == ("first", "last")


def test_public_ssr_post_id_mismatch_is_rejected():
    item = content(["image"])
    item["id_str"] = "999"
    with pytest.raises(b.BilibiliImageError, match="编号"):
        b.page_content(page(item), "opus", ID)


def test_paid_or_blocked_content_is_not_extracted():
    item = content(["image"])
    item["modules"].append({"module_paywall": {"title": "需要权限"}})
    with pytest.raises(b.BilibiliImageError, match="访问权限"):
        b.content_images(item, ID)


def test_old_article_only_extracts_images_inside_body():
    html = '<h1 class="title">专栏<span>标题</span></h1><img src="avatar"><div id="read-article-holder"><h1>正文小标题</h1><p>正文</p><figure><img data-src="//i0.hdslb.com/a.png"></figure><p><img data-src="//i0.hdslb.com/b.png"></p></div><img src="related">'
    post = b.page_content(html, "article", "123")
    assert post.images == ("//i0.hdslb.com/a.png", "//i0.hdslb.com/b.png")
    assert post.title == "专栏标题"


def test_dynamic_opus_title_and_untitled_post():
    item = {"id_str": ID, "modules": {"module_dynamic": {"major": {"opus": {"pics": [{"url": "image"}], "title": "动态标题"}}}}}
    assert b.content_images(item, ID).title == "动态标题"
    assert b.content_images(content(["image"]), ID).title == ""


def test_top_album_dynamic_extracts_five_pictures_and_title_not_ugc_cover(tmp_path, monkeypatch):
    urls = [f"https://i0.hdslb.com/bfs/new_dyn/{i}.jpg" for i in range(5)]
    item = {
        "id_str": ID,
        "modules": [
            {"module_type": "MODULE_TYPE_TOP", "module_top": {"display": {"type": 1, "album": {"pics": [{"url": url, "live_url": ""} for url in urls]}}}},
            {"module_type": "MODULE_TYPE_TITLE", "module_title": {"text": "相册标题"}},
            {"module_type": "MODULE_TYPE_CONTENT", "module_content": {"paragraphs": [
                {"para_type": 1, "text": {"nodes": []}},
                {"para_type": 6, "link_card": {"card": {"type": "LINK_CARD_TYPE_UGC", "ugc": {"cover": "video-cover", "jump_url": "video-link"}}}},
            ]}},
        ],
    }
    monkeypatch.setattr(b.httpx, "get", Mock(return_value=httpx.Response(200, text=page(item), request=httpx.Request("GET", URL))))
    seen = []
    def asset(url, output, timeout):
        seen.append(url)
        Image.new("RGB", (30, 40), "orange").save(output, "JPEG")
    monkeypatch.setattr(b, "download_image", asset)
    images, _ = d.download_to_mp4(URL, tmp_path / "job", 30)
    assert seen == urls
    assert len(images) == 5
    assert images.title == "相册标题"


def test_long_article_body_images_take_precedence_over_album_preview():
    item = content(["first", "second"])
    item["modules"].insert(0, {"module_top": {"display": {"album": {"pics": [{"url": "cover"}, {"url": "first"}]}}}})
    assert b.content_images(item, ID).images == ("first", "second")


def test_unrecognized_nonempty_album_is_an_error_not_silently_ignored():
    item = {"id_str": ID, "modules": [{"module_top": {"display": {"album": {"pics": [{"unexpected": "picture"}]}}}}]}
    with pytest.raises(b.BilibiliImageError, match="包含相册"):
        b.content_images(item, ID)


def test_b23_redirect_can_reach_opus_before_video_downloader():
    with patch.object(b.httpx, "get", return_value=httpx.Response(302, headers={"location": URL}, request=httpx.Request("GET", "https://b23.tv/abc"))) as get:
        assert b.resolve_share_url("https://b23.tv/abc", 10) == URL
    assert get.call_count == 1
    assert get.call_args.kwargs["follow_redirects"] is False


def test_b23_failure_preserves_original_video_route():
    with patch.object(b.httpx, "get", side_effect=httpx.ConnectError("reset")):
        assert b.resolve_share_url("https://b23.tv/abc", 10) == "https://b23.tv/abc"


def test_b23_rejects_external_redirect_before_connection():
    with patch.object(b.httpx, "get", return_value=httpx.Response(302, headers={"location": "https://evil.test/a"}, request=httpx.Request("GET", "https://b23.tv/abc"))) as get:
        with pytest.raises(b.BilibiliImageError, match="不受信任"):
            b.resolve_share_url("https://b23.tv/abc", 10)
    assert get.call_count == 1


def test_api_fallback_routes_video_dynamic_to_existing_video_pipeline(tmp_path, monkeypatch):
    item = {"id_str": ID, "modules": {"module_dynamic": {"major": {"archive": {"bvid": "BV123456", "cover": "cover"}}}}}
    monkeypatch.setattr(b.httpx, "get", Mock(side_effect=[
        httpx.Response(200, text="<html>shell</html>", request=httpx.Request("GET", URL)),
        httpx.Response(200, json={"code": 0, "data": {"item": item}}, request=httpx.Request("GET", "https://api.bilibili.com")),
    ]))
    monkeypatch.setattr(d, "ensure_runtime", lambda platform: ["python", "-m", "yutto"])
    def run(args, **kwargs):
        assert args[-1] == "https://www.bilibili.com/video/BV123456"
        (tmp_path / "job" / "video.mp4").write_bytes(b"video")
        return Mock(returncode=0, stdout="ok", stderr="")
    monkeypatch.setattr(d, "_run_subprocess", run)
    output, _ = d.download_to_mp4(URL, tmp_path / "job", 30)
    assert output.name == "video.mp4"


def test_api_verification_is_clear_without_signed_query_leaks(monkeypatch):
    monkeypatch.setattr(b.httpx, "get", Mock(side_effect=[
        httpx.Response(200, text="shell", request=httpx.Request("GET", URL)),
        httpx.Response(200, json={"code": -352, "message": "private"}, request=httpx.Request("GET", "https://api.bilibili.com/?token=secret")),
    ]))
    with pytest.raises(b.BilibiliImageError, match="访问验证") as error:
        b.fetch_post(URL, 30)
    assert "secret" not in str(error.value)


def test_text_only_post_is_silently_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr(b.httpx, "get", Mock(return_value=httpx.Response(200, text=page(content([])), request=httpx.Request("GET", URL))))
    with pytest.raises(d.NoVideoError):
        d.download_to_mp4(URL, tmp_path / "job", 30)


@pytest.mark.parametrize("url", ["https://evil.test/a", "https://hdslb.com.evil.test/a", "https://127.0.0.1/a", "https://user:pw@i0.hdslb.com/a", "https://i0.hdslb.com:9999/a"])
def test_media_host_must_be_trusted(url):
    with pytest.raises(b.BilibiliImageError):
        b.trusted_image_url(url)


def test_media_redirect_checked_before_request_and_http_upgraded(tmp_path, monkeypatch):
    calls = []
    @contextmanager
    def stream(method, url, **kwargs):
        calls.append(url)
        assert kwargs["follow_redirects"] is False
        assert "cookies" not in kwargs
        yield httpx.Response(302, headers={"location": "https://evil.test/a"}, request=httpx.Request("GET", url))
    monkeypatch.setattr(b.httpx, "stream", stream)
    with pytest.raises(b.BilibiliImageError):
        b.download_image("http://i0.hdslb.com/a", tmp_path / "image", 10)
    assert calls == ["https://i0.hdslb.com/a"]

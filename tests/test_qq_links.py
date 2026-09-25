from qq_bot.links import extract_bilibili_urls, extract_video_urls


def test_extracts_mobile_share_text() -> None:
    message = "【视频标题】 https://b23.tv/AbC123 复制后打开哔哩哔哩"
    assert extract_bilibili_urls(message) == ["https://b23.tv/AbC123"]


def test_extracts_json_card_data() -> None:
    payload = {"type": "json", "data": {"data": '{"meta":{"detail":{"qqdocurl":"https://www.bilibili.com/video/BV1xx411c7mD?p=2"}}}'}}
    assert extract_bilibili_urls(payload) == ["https://www.bilibili.com/video/BV1xx411c7mD?p=2"]


def test_deduplicates_and_ignores_other_sites() -> None:
    payload = ["https://example.com/a", "https://b23.tv/xyz。", {"url": "https://b23.tv/xyz"}]
    assert extract_bilibili_urls(payload) == ["https://b23.tv/xyz"]


def test_extracts_escaped_mobile_card_url() -> None:
    payload = {"data": r'{"url":"https:\/\/www.bilibili.com\/video\/BV1xx411c7mD?p=1&amp;t=2"}'}
    assert extract_bilibili_urls(payload) == ["https://www.bilibili.com/video/BV1xx411c7mD?p=1&t=2"]


def test_extracts_x_and_legacy_twitter_status_links() -> None:
    payload = [
        "看看这个 https://x.com/example/status/1234567890?s=20",
        {"url": "https://twitter.com/i/status/9876543210/video/1"},
    ]
    assert extract_video_urls(payload) == [
        "https://x.com/example/status/1234567890?s=20",
        "https://twitter.com/i/status/9876543210/video/1",
    ]


def test_ignores_x_profile_and_lookalike_hosts() -> None:
    payload = ["https://x.com/example", "https://x.com.evil.example/user/status/123"]
    assert extract_video_urls(payload) == []


def test_extracts_xiaoheihe_share_link() -> None:
    url = "https://api.xiaoheihe.cn/v3/bbs/app/api/web/share?h_camp=link&link_id=5a28725c3264"
    assert extract_video_urls(f"小黑盒视频：{url}") == [url]


def test_extracts_douyin_mobile_share_link() -> None:
    message = "5.12 复制打开抖音，看看【示例用户的作品】 https://v.douyin.com/iRNBho6u/ 03/12"
    assert extract_video_urls(message) == ["https://v.douyin.com/iRNBho6u/"]


def test_extracts_xiaohongshu_mobile_share_link() -> None:
    message = "复制打开小红书，看看这个视频 https://xhslink.com/a/qsoHVeD0Liw1 发现更多精彩"
    assert extract_video_urls(message) == ["https://xhslink.com/a/qsoHVeD0Liw1"]


def test_extracts_new_xiaohongshu_cn_share_link() -> None:
    message = "兔子警官 https://xhslink.cn/o/2wom7IQq1d2 留住这段口令，去【小红书】瞅瞅笔记~"
    assert extract_video_urls(message) == ["https://xhslink.cn/o/2wom7IQq1d2"]

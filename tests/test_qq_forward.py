from __future__ import annotations

from qq_bot.qq_forward import flatten_forward


def test_flatten_forward_reads_nickname_and_text() -> None:
    payload = {
        "messages": [
            {
                "sender": {"nickname": "老王", "card": ""},
                "message": [{"type": "text", "data": {"text": "今天 打本吗"}}],
            },
            {
                "sender": {"card": "群主", "nickname": "testplayer"},
                "message": [
                    {"type": "text", "data": {"text": "来 "}},
                    {"type": "at", "data": {"qq": "10000"}},
                    {"type": "text", "data": {"text": " 缺一个"}},
                ],
            },
        ]
    }
    assert flatten_forward(payload) == "老王：今天 打本吗\n群主：来 @10000 缺一个"


def test_flatten_forward_marks_images() -> None:
    payload = {
        "messages": [
            {
                "sender": {"nickname": "A"},
                "message": [{"type": "image", "data": {"url": "http://x"}}],
            }
        ]
    }
    assert flatten_forward(payload) == "A：[图片]"


def test_flatten_forward_truncates_long_records() -> None:
    payload = {
        "messages": [
            {"sender": {"nickname": "A"}, "message": [{"type": "text", "data": {"text": "x" * 100}}]}
            for _ in range(20)
        ]
    }
    text = flatten_forward(payload, limit=250)
    assert "已截断" in text
    assert len(text) < 400


def test_flatten_forward_handles_bad_payload() -> None:
    assert flatten_forward({}) == ""
    assert flatten_forward({"messages": "nope"}) == ""  # type: ignore[arg-type]

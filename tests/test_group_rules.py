from __future__ import annotations

from qq_bot.group_rules import fallback_question, is_concrete_question, strip_mentions

SELF = "10000"


def msg(*segments: tuple[str, dict]) -> list[dict]:
    return [{"type": kind, "data": data} for kind, data in segments]


def test_strip_mentions_requires_bot_at() -> None:
    segments = msg(("at", {"qq": SELF}), ("text", {"text": " 今天天气怎么样"}))
    assert strip_mentions(segments, SELF) == (True, "今天天气怎么样")


def test_strip_mentions_ignores_other_at() -> None:
    segments = msg(("at", {"qq": "12345"}), ("text", {"text": "你好"}))
    assert strip_mentions(segments, SELF) == (False, "")


def test_strip_mentions_drops_text_before_at() -> None:
    segments = msg(("text", {"text": "前言"}), ("at", {"qq": SELF}), ("text", {"text": "问题"}))
    assert strip_mentions(segments, SELF) == (True, "问题")


def test_greeting_is_not_a_question() -> None:
    for text in ["在吗", "你好", "hi", "？", " @ "]:
        assert is_concrete_question(text) is False


def test_short_but_specific_question_counts() -> None:
    assert is_concrete_question("啥时候更新") is True
    assert is_concrete_question("这是啥") is True
    assert is_concrete_question("为什么") is True


def test_question_mark_counts_even_if_short() -> None:
    assert is_concrete_question("图？") is True


def test_fallback_question_reads_cq_mention() -> None:
    assert fallback_question("[CQ:at,qq=10000] 怎么添加账号", {"10000"}) == "怎么添加账号"


def test_fallback_question_reads_text_mention() -> None:
    assert fallback_question("[at:qq=10000] 怎么添加账号", {"10000"}) == "怎么添加账号"


def test_fallback_question_ignores_messages_without_mention() -> None:
    assert fallback_question("普通聊天", {"10000"}) is None
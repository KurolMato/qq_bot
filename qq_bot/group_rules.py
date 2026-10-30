"""Pure rules for group @-mentioned questions (no NoneBot imports)."""

from __future__ import annotations

import re

_GREETINGS = {
    "在吗",
    "在么",
    "在不在",
    "你好",
    "您好",
    "hi",
    "hello",
    "嗨",
    "？",
    "?",
    "嗯",
    "哦",
    "喂",
}


def rank_request(text: str) -> str | None:
    """Translate an explicit leaderboard request into read-only /rank arguments."""
    text = re.sub(r"\s+", "", text).lower()
    short = re.fullmatch(r"(我的|个人)?(今天|今日|昨天|昨日|本周|这周|本月|这个月)?(排行|排行榜|排名|日榜|周榜|月榜|rank)[。！!？?]*", text)
    if short:
        owner, period, noun = short.groups()
        arg = {"昨天": "y", "昨日": "y", "本周": "w", "这周": "w", "本月": "m", "这个月": "m"}.get(period or "", "")
        if not period:
            arg = {"周榜": "w", "月榜": "m"}.get(noun, "")
        return f"me {arg or ('d' if period or noun == '日榜' else '')}".strip() if owner else arg
    if not re.search(r"排行榜|排行|排名|日榜|周榜|月榜", text):
        return None
    # Questions about usage, negative requests and unsupported periods stay chat.
    if any(word in text for word in (
        "不要", "不用", "别发", "别给", "不想", "怎么", "如何", "为什么", "为啥",
        "规则", "原理", "代码", "配置", "屏蔽", "删除", "移入", "恢复",
        "上周", "上月", "去年", "前天", "明天", "下周", "下月", "其他群", "别的群",
    )) or re.search(r"\d+月|\d+日|\d+号", text):
        return None
    if not re.search(r"发|给|看|查|来[一份张个下]|展示|输出", text):
        return None
    periods = [arg for words, arg in (
        (("今天", "今日", "日榜"), "d"),
        (("昨天", "昨日"), "y"),
        (("本周", "这周", "周榜"), "w"),
        (("本月", "这个月", "这月", "月榜"), "m"),
    ) if any(word in text for word in words)]
    if len(periods) > 1:
        return None
    personal = "我的" in text or "个人" in text or bool(re.search(r"我(?:今天|今日|昨天|昨日|本周|这周|本月|这个月)", text))
    period = periods[0] if periods else ""
    if personal:
        return f"me {period}".strip()
    return "" if period == "d" else period


def strip_mentions(segments: list[dict], self_ids: str | set[str] | list[str]) -> tuple[bool, str]:
    """Return (mentions_bot, text after that mention) for OneBot segments."""
    wanted = {str(self_ids)} if isinstance(self_ids, str) else {str(item) for item in self_ids}
    mentioned = False
    texts: list[str] = []
    for segment in segments:
        kind = segment.get("type")
        data = segment.get("data") or {}
        if kind == "at":
            if str(data.get("qq")) in wanted:
                mentioned = True
            continue
        if kind == "text" and mentioned:
            texts.append(str(data.get("text") or ""))
    return mentioned, "".join(texts).strip()


_QUESTION_MARKERS = (
    "?",
    "？",
    "怎么",
    "怎样",
    "如何",
    "为什么",
    "为啥",
    "咋",
    "什么",
    "啥",
    "哪",
    "多少",
    "多久",
    "几点",
    "谁",
    "能不能",
    "可不可以",
    "可以吗",
    "是否",
    "有没有",
    "是不是",
    "行不行",
    "吗",
)


def is_concrete_question(text: str) -> bool:
    """Only actual questions count: greetings, affection and chit-chat stay silent."""
    cleaned = text.strip().strip("，,。.！!~～:：;； ")
    if not cleaned or cleaned.lower() in _GREETINGS:
        return False
    return any(marker in cleaned for marker in _QUESTION_MARKERS)


def is_addressed_request(text: str) -> bool:
    """An addressed request need not contain a question word or question mark."""
    cleaned = text.strip().strip("，,。.！!~～:：;； ")
    return bool(cleaned) and cleaned.lower() not in _GREETINGS


_CQ_AT = re.compile(r"\[CQ:at,qq=\d+\]")
_TEXT_AT = re.compile(r"\[at:qq=\d+\]|@\S+\s*")


def fallback_question(raw: str, self_ids: str | set[str] | list[str]) -> str | None:
    """Recover an @ question when the client delivered the mention as text."""
    wanted = {str(self_ids)} if isinstance(self_ids, str) else {str(item) for item in self_ids}
    if not raw:
        return None
    if not any(f"qq={sid}]" in raw or f'qq="{sid}"' in raw or f"qq={sid}," in raw for sid in wanted):
        return None
    text = _CQ_AT.sub(" ", raw)
    if any(f"qq={sid}" in raw for sid in wanted):
        text = _TEXT_AT.sub(" ", text)
    return text.strip()

"""Flatten QQ merged/forwarded chat records into plain text for the agent."""

from __future__ import annotations

MAX_FORWARD_CHARS = 3000


def _segment_text(segment: dict) -> str:
    kind = segment.get("type")
    data = segment.get("data") or {}
    if kind == "text":
        return str(data.get("text") or "")
    if kind == "at":
        return f"@{data.get('qq')} "
    if kind in {"image", "mface"}:
        return "[图片]"
    if kind == "face":
        return "[表情]"
    return ""


def flatten_forward(payload: dict, limit: int = MAX_FORWARD_CHARS) -> str:
    """Turn a ``get_forward_msg`` result into readable lines."""
    messages = payload.get("messages") if isinstance(payload, dict) else None
    if not isinstance(messages, list):
        return ""
    lines: list[str] = []
    total = 0
    for item in messages:
        if not isinstance(item, dict):
            continue
        sender = item.get("sender") or {}
        nickname = str(sender.get("card") or sender.get("nickname") or sender.get("user_id") or "某人")
        content = "".join(_segment_text(seg) for seg in item.get("message") or [] if isinstance(seg, dict))
        content = " ".join(content.split())
        if not content:
            continue
        line = f"{nickname}：{content}"
        if total + len(line) > limit:
            lines.append("……（转发内容过长，已截断）")
            break
        lines.append(line)
        total += len(line)
    return "\n".join(lines)

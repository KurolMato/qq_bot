from __future__ import annotations


def normalize_nickname(value: str) -> str:
    nickname = " ".join(value.strip().split())
    if not nickname:
        raise ValueError("昵称不能为空。")
    if len(nickname) > 20:
        raise ValueError("昵称不能超过 20 个字符。")
    if any(ord(character) < 32 for character in nickname):
        raise ValueError("昵称中不能包含控制字符。")
    return nickname


def list_line(name: str, current_game: str | None) -> str:
    return f"{name}｜视奸中｜{current_game or '未在游戏'}"

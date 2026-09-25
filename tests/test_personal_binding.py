import sys
from types import SimpleNamespace

import pytest

from datetime import datetime, timezone
from io import BytesIO
from PIL import Image
from qq_bot.game_time_leaderboard import _render, build_leaderboard_message
from qq_bot.game_time_tracker import LeaderboardSnapshot, TrackedMember, TrackedGame

from qq_bot.game_time_tracker import GameTimeTracker
from qq_bot.personal_game_time import resolve_binding_name


@pytest.mark.parametrize("period", ["day", "yesterday", "week", "month"])
def test_personal_image(period, tmp_path):
    snapshot = LeaderboardSnapshot(
        datetime(2026, 9, 12, 4, tzinfo=timezone.utc), None,
        (TrackedMember("示例玩家", None, 12000, (
            TrackedGame("steam", "怪物猎人：崛起", 7200, None),
            TrackedGame("ps", "宇宙机器人", 3600, None),
            TrackedGame("switch", "喷射战士3", 1200, None),
        )),), period,
    )
    content = _render(snapshot, {}, "示例玩家")
    with Image.open(BytesIO(content)) as picture:
        assert picture.size == (900, 492)
    path = tmp_path / f"personal-{period}.png"
    path.write_bytes(content)
    print(path)


@pytest.mark.asyncio
async def test_empty_personal_returns_image():
    snapshot = LeaderboardSnapshot(datetime.now(timezone.utc), None, (), "day")
    message = await build_leaderboard_message(snapshot, personal_name="示例玩家")
    assert len(message) == 1
    assert message[0].type == "image"


def test_binding_replaces_and_persists_with_group_user_isolation(tmp_path):
    path = tmp_path / "time.db"
    tracker = GameTimeTracker(path)
    assert tracker.bound_member("g1", "q1") is None
    tracker.bind_qq_member("g1", "q1", "示例玩家")
    tracker.bind_qq_member("g1", "q1", "示例玩家")
    tracker.bind_qq_member("g1", "q2", "Other")
    tracker.bind_qq_member("g2", "q1", "Elsewhere")
    tracker.bind_qq_member("g1", "q1", "New Name")
    with pytest.raises(ValueError):
        tracker.bind_qq_member("g1", "q1", " ")
    reopened = GameTimeTracker(path)
    assert reopened.bound_member("g1", "q1") == "New Name"
    assert reopened.bound_member("g1", "q2") == "Other"
    assert reopened.bound_member("g2", "q1") == "Elsewhere"


def test_binding_resolves_platform_names_and_shared_nicknames(monkeypatch):
    records = {
        "steam_registry": [SimpleNamespace(nickname="示例玩家", display_name="Steam Player"),
                           SimpleNamespace(nickname="示例玩家 Two", display_name="Other")],
        "ps5_registry": [SimpleNamespace(nickname="示例玩家", online_id="ExamplePSN")],
        "switch_registry": [SimpleNamespace(nickname="小明", ns_name="Switch Player")],
        "xbox_registry": [SimpleNamespace(nickname="", gamertag="Xbox Player")],
    }
    for module, entries in records.items():
        registry = SimpleNamespace(list_group=lambda group, rows=entries: rows if group == "g1" else [])
        monkeypatch.setitem(sys.modules, "qq_bot." + module, SimpleNamespace(registry=registry))
    assert resolve_binding_name("g1", "示例玩家") == "示例玩家"
    assert resolve_binding_name("g1", "ExamplePSN") == "示例玩家"
    assert resolve_binding_name("g1", "steam player") == "示例玩家"
    assert resolve_binding_name("g1", "Switch") == "小明"
    assert resolve_binding_name("g1", "Xbox Player") == "Xbox Player"
    with pytest.raises(ValueError, match="多个成员"):
        resolve_binding_name("g1", "mat")
    with pytest.raises(ValueError, match="没有找到"):
        resolve_binding_name("g2", "示例玩家")
    with pytest.raises(ValueError, match="用法"):
        resolve_binding_name("g1", " ")

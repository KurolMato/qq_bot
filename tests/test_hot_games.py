import asyncio
from datetime import datetime, timedelta
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import nonebot
import pytest
from PIL import Image
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, PrivateMessageEvent
from nonebot.consts import CMD_ARG_KEY, PREFIX_KEY

from qq_bot.game_time_tracker import GameTimeTracker, LOCAL_TZ, TrackedGame, TrackedMember
from qq_bot.hot_games import HotGames, HotGame, aggregate_hot_games, build_hot_games_data, hot_game_ranks
from qq_bot.hot_games_card import render_hot_games


def test_players_not_hours_and_cross_platform_dedup():
    def game(name, seconds=60, platform="steam"):
        return TrackedGame(platform, name, seconds, None)
    snapshot = SimpleNamespace(period="week", members=[
        TrackedMember("Alice", None, 0, (game("Popular"), game("popular", platform="ps5"), game("Long", 99999))),
        TrackedMember("Bob", None, 0, (game("Popular"), game("Zero", 0))),
        TrackedMember("ALICE", None, 0, (game("Popular"),)),
    ])
    rows = aggregate_hot_games(snapshot).games
    assert [(g.name, g.players) for g in rows] == [("Popular", 2), ("Long", 1)]


def test_bilingual_cross_platform_unique_people():
    snapshot = SimpleNamespace(period="month", members=(
        TrackedMember("Alice", None, 0, (
            TrackedGame("steam", "Phantom Blade Zero™", 60, None),
            TrackedGame("ps5", "影之刃零", 90, None))),
        TrackedMember("Bob", None, 0, (
            TrackedGame("ps5", "影之刃 零", 90, None),)),
        TrackedMember("Carol", None, 0, (
            TrackedGame("steam", "ＰＨＡＮＴＯＭ BLADE ZERO", 60, None),)),
        TrackedMember("Dan", None, 0, (
            TrackedGame("steam", "Phantom Blade Zero 2", 60, None),)),
    ))
    games = aggregate_hot_games(snapshot).games
    assert [(g.name, g.players) for g in games] == [("影之刃零", 3), ("Phantom Blade Zero 2", 1)]


def test_history_title_identity_is_readonly_and_group_scoped(tmp_path):
    import sqlite3
    from qq_bot.hot_game_identity import title_identities, title_key
    path = tmp_path / "aliases.db"
    with sqlite3.connect(path) as db:
        for table in ("game_time_buckets", "game_time_accounts"):
            db.execute(f"CREATE TABLE {table} (platform, game_key, game_name, group_id)")
        db.executemany("INSERT INTO game_time_buckets VALUES (?,?,?,?)", [
            ("steam", "100", "Example Game", "g"),
            ("steam", "100", "示例游戏", "g"),
            ("ps5", "200", "示例游戏", "g"),
            ("ps5", "200", "Example Regional Name", "g"),
            ("ps5", "100", "Different Game", "g"),
            ("steam", "100", "Other Group Title", "other"),
        ])
    before = path.read_bytes()
    identities = title_identities(path, "g")
    assert identities[title_key("Example Game")] == "示例游戏"
    assert identities[title_key("Example Regional Name")] == "示例游戏"
    assert identities[title_key("Different Game")] == "Different Game"
    assert title_key("Other Group Title") not in identities
    assert path.read_bytes() == before
    snapshot = SimpleNamespace(period="week", members=(
        TrackedMember("Alice", None, 0, (
            TrackedGame("steam", "Example Game", 60, None),
            TrackedGame("ps5", "Example Regional Name", 60, None))),
        TrackedMember("Bob", None, 0, (TrackedGame("ps5", "示例游戏", 60, None),)),
    ))
    assert [(g.name, g.players) for g in aggregate_hot_games(snapshot, identities).games] == [("示例游戏", 2)]
    assert title_identities(tmp_path / "missing.db")[title_key("phantom blade zero")] == "影之刃零"
    assert not (tmp_path / "missing.db").exists()


@pytest.mark.parametrize("counts,expected", [
    ([10, 9, 8, 7, 6, 5, 5, 5, 4], 8),
    ([10, 9, 8, 7, 6, 5, 4], 6),
    ([2] * 9, 9),
])
def test_sixth_position_includes_all_ties(counts, expected):
    members = tuple(TrackedMember(str(player), None, 0, tuple(
        TrackedGame("steam", f"Game {i:02}", 60, None)
        for i, count in enumerate(counts) if player < count
    )) for player in range(max(counts)))
    data = aggregate_hot_games(SimpleNamespace(period="week", members=members))
    assert len(data.games) == expected
    assert [game.players for game in data.games] == counts[:expected]


def test_competition_ranks_and_medals_beyond_three_slots(monkeypatch):
    from qq_bot import hot_games_card
    rows = tuple(HotGame("steam", str(i), 0, None, count)
                 for i, count in enumerate([10, 9, 8, 8, 8, 7, 7, 7]))
    assert hot_game_ranks(rows) == (1, 2, 3, 3, 3, 6, 6, 6)
    medals = []
    monkeypatch.setattr(hot_games_card, "_metal_rim", lambda canvas, box, place: medals.append(place))
    with Image.open(BytesIO(render_hot_games(HotGames("week", rows), {}))) as image:
        assert image.height == 510 + 5 * 94
    assert medals == [0, 1, 2, 2, 2]
    medals.clear()
    rows = tuple(HotGame("steam", str(i), 0, None, 2) for i in range(8))
    assert hot_game_ranks(rows) == (1,) * 8
    render_hot_games(HotGames("week", rows), {})
    assert medals == [0] * 8


def test_all_tied_covers_loaded_in_batches(monkeypatch):
    from dataclasses import replace
    from qq_bot import hot_games, personal_rank_assets, hot_games_card
    rows = tuple(HotGame("steam", str(i), 0, None, 2) for i in range(8))
    data = HotGames("week", rows)
    monkeypatch.setattr(hot_games, "build_hot_games_data", lambda *a: data)
    async def load(batch):
        return replace(batch, games=tuple(replace(g, image_url=g.name) for g in batch.games)), {
            g.name: b"fixture" for g in batch.games}
    loader = AsyncMock(side_effect=load)
    monkeypatch.setattr(personal_rank_assets, "load_personal_rank_assets", loader)
    captured = []
    real_render = hot_games_card.render_hot_games
    def render(loaded, assets):
        captured.append((loaded, assets))
        return real_render(loaded, assets)
    monkeypatch.setattr(hot_games_card, "render_hot_games", render)
    asyncio.run(hot_games.build_hot_games_message(None, "g"))
    assert [len(call.args[0].games) for call in loader.call_args_list] == [6, 2]
    assert len(captured[0][0].games) == len(captured[0][1]) == 8
    assert all(g.image_url == g.name for g in captured[0][0].games)


def test_period_group_filter_and_hidden_games(tmp_path):
    tracker = GameTimeTracker(tmp_path / "games.db")
    monday = datetime(2026, 11, 2, 4, tzinfo=LOCAL_TZ)
    def play(group, account, name, game, at):
        for instant in (at, at + timedelta(minutes=2)):
            tracker.observe(platform="steam", account_id=account, group_id=group,
                            display_name=name, avatar_url=None, game_key=game,
                            game_name=game, game_image_url=None, observed_at=instant)
    play("g", "a", "Alice", "Previous week", monday - timedelta(hours=1))
    play("g", "a", "Alice", "Current", monday)
    play("g", "b", "Bob", "Current", monday)
    play("other", "c", "Charlie", "Current", monday)
    tracker.move_member_to_other_rank("g", "Bob")
    week = build_hot_games_data(tracker, "g", at=monday + timedelta(hours=1))
    assert [(g.name, g.players) for g in week.games] == [("Current", 2)]
    month = build_hot_games_data(tracker, "g", "month", at=monday + timedelta(hours=1))
    assert len(month.games) == 2
    assert len(build_hot_games_data(tracker, "g", at=monday - timedelta(minutes=1)).games) == 1
    tracker.hide_game("g", "Current")
    assert not build_hot_games_data(tracker, "g", at=monday + timedelta(hours=1)).games
    assert not build_hot_games_data(tracker, "g", "month", at=datetime(2026, 12, 1, 4, tzinfo=LOCAL_TZ)).games
    # A stale poll must not make next week's hot list display last week's data.
    tracker.mark_poll("steam", monday)
    assert not build_hot_games_data(tracker, "g", at=monday + timedelta(days=7)).games


@pytest.mark.parametrize("length", [0, 1, 2, 3, 6])
def test_render_missing_corrupt_art_and_long_titles(length):
    games = tuple(HotGame("steam", "超长游戏标题" * 12, 0, "broken", 1234) for _ in range(length))
    raw = render_hot_games(HotGames("month", games), {"broken": b"invalid"})
    with Image.open(BytesIO(raw)) as image:
        assert image.width == 900
        assert image.height >= 330
        image.verify()


@pytest.mark.parametrize(("argument", "period"), [("", "week"), ("m", "month"), (" M ", "month")])
def test_command_dispatch(monkeypatch, argument, period):
    nonebot.init()
    from qq_bot import game_time_commands as commands
    monkeypatch.setattr(commands, "config", SimpleNamespace(allowed_groups={"123"}))
    builder = AsyncMock(return_value=Message("image placeholder"))
    monkeypatch.setattr(commands, "build_hot_games_message", builder)
    bot = SimpleNamespace(send=AsyncMock())
    event = SimpleNamespace(group_id=123)
    asyncio.run(commands.handle_hot_command(bot, event, Message(argument)))
    builder.assert_awaited_once_with(commands.tracker, "123", period)
    bot.send.assert_awaited_once()
    assert not commands._pending_hot


@pytest.mark.parametrize(("group", "argument", "period"), [
    (True, "", "week"), (True, "m", "month"),
    (False, "", "week"), (False, "m", "month"),
])
def test_hot_matcher_routes_group_and_owner_private_messages(monkeypatch, group, argument, period):
    nonebot.init()
    from qq_bot import game_time_commands as commands

    monkeypatch.setenv("QQ_DSH_ENABLED", "true")
    monkeypatch.setenv("QQ_DSH_USERS", "1")
    monkeypatch.setenv("QQ_DSH_GROUPS", "123")
    monkeypatch.setenv("QQ_ALLOWED_GROUPS", "123")
    monkeypatch.setattr(commands, "config", SimpleNamespace(allowed_groups={"123"}))
    reply = Message("image placeholder")
    builder = AsyncMock(return_value=reply)
    monkeypatch.setattr(commands, "build_hot_games_message", builder)
    send = AsyncMock()
    monkeypatch.setattr(Bot, "send", send)
    bot = Bot(SimpleNamespace(get_name=lambda: "OneBot V11"), "10000")
    text = f"/hot {argument}".strip()
    fields = dict(time=0, self_id=10000, post_type="message", user_id=1, message_id=1,
                  message=Message(text), original_message=Message(text), raw_message=text, font=0, sender={})
    if group:
        event = GroupMessageEvent(**fields, message_type="group", sub_type="normal", group_id=123)
    else:
        event = PrivateMessageEvent(**fields, message_type="private", sub_type="friend")

    asyncio.run(commands.hot_command().run(bot, event, {PREFIX_KEY: {CMD_ARG_KEY: Message(argument)}}))

    builder.assert_awaited_once_with(commands.tracker, "123", period)
    send.assert_awaited_once_with(event, reply)


@pytest.mark.parametrize(("enabled", "sender"), [("true", 2), ("false", 1)])
def test_private_hot_requires_enabled_owner_chat(monkeypatch, enabled, sender):
    nonebot.init()
    from qq_bot import game_time_commands as commands

    monkeypatch.setenv("QQ_DSH_ENABLED", enabled)
    monkeypatch.setenv("QQ_DSH_USERS", "1")
    build = AsyncMock()
    monkeypatch.setattr(commands, "build_hot_games_message", build)
    bot = SimpleNamespace(send=AsyncMock())
    asyncio.run(commands.handle_private_hot_command(bot, SimpleNamespace(user_id=sender), Message("m")))
    build.assert_not_awaited()
    bot.send.assert_not_awaited()


def test_invalid_disallowed_and_failure(monkeypatch):
    nonebot.init()
    from qq_bot import game_time_commands as commands
    monkeypatch.setattr(commands, "config", SimpleNamespace(allowed_groups={"123"}))
    builder = AsyncMock(side_effect=RuntimeError("fixture failure"))
    monkeypatch.setattr(commands, "build_hot_games_message", builder)
    bot = SimpleNamespace(send=AsyncMock())
    asyncio.run(commands.handle_hot_command(bot, SimpleNamespace(group_id=999), Message()))
    bot.send.assert_not_awaited()
    asyncio.run(commands.handle_hot_command(bot, SimpleNamespace(group_id=123), Message("m 9")))
    builder.assert_not_awaited()
    assert "用法" in str(bot.send.call_args.args[1])
    asyncio.run(commands.handle_hot_command(bot, SimpleNamespace(group_id=123), Message()))
    assert "失败" in str(bot.send.call_args.args[1])
    assert not commands._pending_hot


def test_image_message_pipeline(monkeypatch):
    from qq_bot import hot_games, personal_rank_assets
    data = HotGames("week", (HotGame("steam", "Game", 0, None, 2),))
    monkeypatch.setattr(hot_games, "build_hot_games_data", lambda *a: data)
    monkeypatch.setattr(personal_rank_assets, "load_personal_rank_assets", AsyncMock(return_value=(data, {})))
    message = asyncio.run(hot_games.build_hot_games_message(None, "g"))
    assert message[0].type == "image"

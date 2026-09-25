from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

from PIL import Image

from qq_bot.game_time_leaderboard import _leaderboard_cover, _period_play_label, _render
from qq_bot.game_time_tracker import (
    GameTimeTracker,
    LeaderboardSnapshot,
    TrackedGame,
    TrackedMember,
    day_start,
    month_start,
)


UTC = timezone.utc


def test_yesterday_settlement_splits_midnight_and_uses_wall_clock(tmp_path):
    tracker = GameTimeTracker(tmp_path / "time.db")
    local = timezone(timedelta(hours=8))
    before = datetime(2026, 9, 1, 3, 59, tzinfo=local)
    after = before + timedelta(minutes=2)
    _observe(tracker, platform="steam", account="a", name="TestPlayer", game="Game", at=before)
    _observe(tracker, platform="steam", account="a", name="TestPlayer", game="Game", at=after)
    tracker.mark_poll("steam", before)  # stale poll does not change which day is yesterday
    result = tracker.yesterday_snapshot("200", at=after)
    assert result.day_start == datetime(2026, 8, 31, 4, tzinfo=local)
    assert result.last_poll == datetime(2026, 9, 1, 4, tzinfo=local)
    assert result.members[0].total_seconds == 60
    assert result.period == "yesterday"
    tracker.move_member_to_other_rank("200", "TestPlayer")
    assert not tracker.yesterday_snapshot("200", at=after).members
    assert tracker.yesterday_snapshot("200", at=after, board="other").members[0].total_seconds == 60
    assert not tracker.yesterday_snapshot("other-group", at=after, board="other").members


def test_personal_merges_platforms_and_ignores_board_but_filters_games(tmp_path):
    tracker = GameTimeTracker(tmp_path / "time.db")
    start = datetime(2026, 9, 12, 5, tzinfo=timezone(timedelta(hours=8)))
    for platform, game in (("steam", "Game"), ("switch", "Hidden")):
        _observe(tracker, platform=platform, account=platform, name="TestPlayer", game=game, at=start)
        _observe(tracker, platform=platform, account=platform, name="TestPlayer", game=game, at=start + timedelta(minutes=2))
    tracker.mark_poll("steam", start + timedelta(minutes=2))
    tracker.move_member_to_other_rank("200", "TestPlayer")
    for period in ("day", "week", "month"):
        assert tracker.personal_snapshot("200", "TestPlayer", period=period, at=start).members[0].total_seconds == 240
    tracker.hide_game("200", "Hidden")
    reopened = GameTimeTracker(tmp_path / "time.db")
    result = reopened.personal_snapshot("200", "Mat", at=start)
    assert result.members[0].total_seconds == 120
    assert len(result.members[0].games) == 1


def test_personal_query_parsing_and_empty_result():
    import pytest
    from qq_bot.personal_game_time import parse_personal_query, format_personal
    assert parse_personal_query("w Name With Spaces") == ("week", "Name With Spaces")
    assert parse_personal_query("Name With Spaces") == ("day", "Name With Spaces")
    with pytest.raises(ValueError):
        parse_personal_query("w")
    assert "昨日暂无" in format_personal(
        LeaderboardSnapshot(datetime(2026, 9, 1, tzinfo=UTC), None, (), "yesterday"), "TestPlayer"
    )


def _observe(
    tracker: GameTimeTracker,
    *,
    platform: str,
    account: str,
    name: str,
    game: str | None,
    at: datetime,
) -> None:
    tracker.observe(
        platform=platform,
        account_id=account,
        group_id="200",
        display_name=name,
        avatar_url=f"https://example.com/{account}.png",
        game_key=game.casefold() if game else None,
        game_name=game,
        game_image_url=f"https://example.com/{game}.jpg" if game else None,
        observed_at=at,
    )


def test_cross_platform_time_merges_by_unique_nickname(tmp_path: Path) -> None:
    tracker = GameTimeTracker(tmp_path / "time.db", max_sample_gap=600)
    start = datetime(2026, 8, 29, 4, 10, tzinfo=timezone(timedelta(hours=8))).astimezone(UTC)
    _observe(tracker, platform="switch", account="sw", name="TestPlayer", game="Game A", at=start)
    _observe(tracker, platform="steam", account="st", name="示例玩家", game="Game B", at=start)
    _observe(tracker, platform="switch", account="sw", name="TestPlayer", game="Game A", at=start + timedelta(minutes=2))
    _observe(tracker, platform="steam", account="st", name="示例玩家", game="Game B", at=start + timedelta(minutes=3))
    tracker.mark_poll("steam", start + timedelta(minutes=3))

    snapshot = tracker.snapshot("200")

    assert len(snapshot.members) == 1
    member = snapshot.members[0]
    assert member.name == "示例玩家"
    assert member.total_seconds == 300
    assert [(game.name, game.seconds) for game in member.games] == [
        ("Game B", 180),
        ("Game A", 120),
    ]


def test_interval_crossing_0400_is_split_into_correct_day(tmp_path: Path) -> None:
    tracker = GameTimeTracker(tmp_path / "time.db", max_sample_gap=300)
    local = timezone(timedelta(hours=8))
    before = datetime(2026, 8, 29, 3, 59, tzinfo=local).astimezone(UTC)
    after = datetime(2026, 8, 29, 4, 1, tzinfo=local).astimezone(UTC)
    _observe(tracker, platform="steam", account="st", name="TestPlayer", game="Game", at=before)
    _observe(tracker, platform="steam", account="st", name="TestPlayer", game="Game", at=after)
    tracker.mark_poll("steam", after)

    snapshot = tracker.snapshot("200")

    assert snapshot.day_start.date().isoformat() == "2026-08-29"
    assert snapshot.members[0].total_seconds == 60


def test_long_outage_gap_is_not_counted(tmp_path: Path) -> None:
    tracker = GameTimeTracker(tmp_path / "time.db", max_sample_gap=300)
    start = datetime(2026, 8, 29, 5, 0, tzinfo=timezone(timedelta(hours=8))).astimezone(UTC)
    _observe(tracker, platform="ps", account="ps", name="TestPlayer", game="Game", at=start)
    _observe(tracker, platform="ps", account="ps", name="TestPlayer", game="Game", at=start + timedelta(minutes=10))
    tracker.mark_poll("ps", start + timedelta(minutes=10))

    assert tracker.snapshot("200").members == ()


def test_resolved_cover_backfills_existing_current_day_bucket(tmp_path: Path) -> None:
    tracker = GameTimeTracker(tmp_path / "time.db", max_sample_gap=300)
    start = datetime(2026, 8, 29, 5, 0, tzinfo=timezone(timedelta(hours=8))).astimezone(UTC)
    tracker.observe(
        platform="steam",
        account_id="st",
        group_id="200",
        display_name="TestPlayer",
        avatar_url=None,
        game_key="123",
        game_name="Game",
        game_image_url="https://example.com/missing.jpg",
        observed_at=start,
    )
    tracker.observe(
        platform="steam",
        account_id="st",
        group_id="200",
        display_name="TestPlayer",
        avatar_url=None,
        game_key="123",
        game_name="Game",
        game_image_url="https://example.com/resolved.jpg",
        observed_at=start + timedelta(minutes=2),
    )
    tracker.mark_poll("steam", start + timedelta(minutes=2))

    game = tracker.snapshot("200").members[0].games[0]
    assert game.image_url == "https://example.com/resolved.jpg"


def test_rank_hidden_game_is_excluded_but_activity_time_remains_stored(tmp_path: Path) -> None:
    tracker = GameTimeTracker(tmp_path / "time.db", max_sample_gap=600)
    start = datetime(2026, 8, 29, 5, 0, tzinfo=timezone(timedelta(hours=8))).astimezone(UTC)
    _observe(tracker, platform="steam", account="st", name="TestPlayer", game="Hidden Game", at=start)
    _observe(
        tracker,
        platform="steam",
        account="st",
        name="TestPlayer",
        game="Visible Game",
        at=start + timedelta(minutes=2),
    )
    _observe(
        tracker,
        platform="steam",
        account="st",
        name="TestPlayer",
        game="Visible Game",
        at=start + timedelta(minutes=4),
    )
    tracker.mark_poll("steam", start + timedelta(minutes=4))

    assert tracker.hide_game("200", "Hidden Game") is True
    snapshot = tracker.snapshot("200")

    assert snapshot.members[0].total_seconds == 120
    assert [game.name for game in snapshot.members[0].games] == ["Visible Game"]
    assert tracker.unhide_game("200", "Hidden Game") is True
    assert len(tracker.snapshot("200").members[0].games) == 2


def test_rank_hidden_game_names_are_case_and_separator_insensitive(tmp_path: Path) -> None:
    tracker = GameTimeTracker(tmp_path / "time.db")
    assert tracker.hide_game("200", "Bingo Cat") is True
    assert tracker.hide_game("200", "bingo-cat") is False
    assert tracker.unhide_game("200", "BINGO CAT") is True


def test_rank_hidden_game_supports_abbreviation_fuzzy_match(tmp_path: Path) -> None:
    tracker = GameTimeTracker(tmp_path / "time.db", max_sample_gap=600)
    start = datetime(2026, 8, 29, 5, 0, tzinfo=timezone(timedelta(hours=8))).astimezone(UTC)
    _observe(
        tracker,
        platform="steam",
        account="st",
        name="TestPlayer",
        game="TBH 塔斯巴克英雄",
        at=start,
    )
    _observe(
        tracker,
        platform="steam",
        account="st",
        name="TestPlayer",
        game="TBH 塔斯巴克英雄",
        at=start + timedelta(minutes=2),
    )
    tracker.mark_poll("steam", start + timedelta(minutes=2))

    assert tracker.hide_game("200", "TBH") is True
    assert tracker.snapshot("200").members == ()
    assert tracker.unhide_game("200", "TBH") is True
    assert tracker.snapshot("200").members[0].games[0].name == "TBH 塔斯巴克英雄"


def test_other_rank_moves_merged_member_out_of_main_board(tmp_path: Path) -> None:
    tracker = GameTimeTracker(tmp_path / "time.db", max_sample_gap=600)
    start = datetime(2026, 9, 9, 5, 0, tzinfo=timezone(timedelta(hours=8))).astimezone(UTC)
    _observe(tracker, platform="steam", account="st", name="TestPlayer", game="Game A", at=start)
    _observe(tracker, platform="switch", account="sw", name="示例玩家", game="Game B", at=start)
    _observe(tracker, platform="steam", account="st", name="TestPlayer", game="Game A", at=start + timedelta(minutes=2))
    _observe(tracker, platform="switch", account="sw", name="示例玩家", game="Game B", at=start + timedelta(minutes=2))
    tracker.mark_poll("steam", start + timedelta(minutes=2))

    display_name, changed = tracker.move_member_to_other_rank("200", "TestPlayer")

    assert display_name in {"TestPlayer", "示例玩家"}
    assert changed is True
    assert tracker.snapshot("200").members == ()
    other = tracker.snapshot("200", board="other")
    assert len(other.members) == 1
    assert other.members[0].total_seconds == 240
    assert len(other.members[0].games) == 2

    _, restored = tracker.restore_member_to_main_rank("200", "示例玩家")
    assert restored is True
    assert tracker.snapshot("200", board="other").members == ()
    assert len(tracker.snapshot("200").members) == 1


def test_other_rank_supports_unique_partial_member_match_and_all_periods(tmp_path: Path) -> None:
    tracker = GameTimeTracker(tmp_path / "time.db", max_sample_gap=600)
    start = datetime(2026, 9, 9, 5, 0, tzinfo=timezone(timedelta(hours=8))).astimezone(UTC)
    _observe(tracker, platform="steam", account="st", name="Scarlet_TestPlayer", game="Game", at=start)
    _observe(tracker, platform="steam", account="st", name="Scarlet_TestPlayer", game="Game", at=start + timedelta(minutes=2))
    tracker.mark_poll("steam", start + timedelta(minutes=2))

    tracker.move_member_to_other_rank("200", "Scarlet")

    assert len(tracker.snapshot("200", board="other").members) == 1
    assert len(tracker.weekly_snapshot("200", board="other").members) == 1
    assert len(tracker.monthly_snapshot("200", board="other").members) == 1


def test_monthly_rank_aggregates_current_month_and_excludes_previous_month(tmp_path: Path) -> None:
    tracker = GameTimeTracker(tmp_path / "time.db", max_sample_gap=600)
    local = timezone(timedelta(hours=8))
    august = datetime(2026, 8, 31, 5, 0, tzinfo=local).astimezone(UTC)
    september_first = datetime(2026, 9, 1, 5, 0, tzinfo=local).astimezone(UTC)
    september_second = datetime(2026, 9, 2, 5, 0, tzinfo=local).astimezone(UTC)

    _observe(tracker, platform="steam", account="st", name="TestPlayer", game="Old Game", at=august)
    _observe(
        tracker,
        platform="steam",
        account="st",
        name="TestPlayer",
        game="Old Game",
        at=august + timedelta(minutes=2),
    )
    _observe(tracker, platform="steam", account="st", name="TestPlayer", game="Month Game", at=september_first)
    _observe(
        tracker,
        platform="steam",
        account="st",
        name="TestPlayer",
        game="Month Game",
        at=september_first + timedelta(minutes=3),
    )
    _observe(tracker, platform="steam", account="st", name="TestPlayer", game="Month Game", at=september_second)
    _observe(
        tracker,
        platform="steam",
        account="st",
        name="TestPlayer",
        game="Month Game",
        at=september_second + timedelta(minutes=4),
    )
    tracker.mark_poll("steam", september_second + timedelta(minutes=4))

    snapshot = tracker.monthly_snapshot("200")

    assert snapshot.period == "month"
    assert snapshot.day_start == datetime(2026, 9, 1, 4, 0, tzinfo=local)
    assert snapshot.members[0].total_seconds == 420
    assert [game.name for game in snapshot.members[0].games] == ["Month Game"]
    assert tracker.snapshot("200").members[0].total_seconds == 240

    august_snapshot = tracker.monthly_snapshot("200", at=september_second, month=8)
    assert august_snapshot.day_start == datetime(2026, 8, 1, 4, 0, tzinfo=local)
    assert august_snapshot.last_poll == august + timedelta(minutes=2)
    assert august_snapshot.members[0].total_seconds == 120
    assert [game.name for game in august_snapshot.members[0].games] == ["Old Game"]


def test_monthly_rank_applies_fuzzy_hidden_games(tmp_path: Path) -> None:
    tracker = GameTimeTracker(tmp_path / "time.db", max_sample_gap=600)
    start = datetime(2026, 9, 1, 5, 0, tzinfo=timezone(timedelta(hours=8))).astimezone(UTC)
    _observe(tracker, platform="steam", account="st", name="TestPlayer", game="TBH 塔斯巴克英雄", at=start)
    _observe(
        tracker,
        platform="steam",
        account="st",
        name="TestPlayer",
        game="Visible Game",
        at=start + timedelta(minutes=2),
    )
    _observe(
        tracker,
        platform="steam",
        account="st",
        name="TestPlayer",
        game="Visible Game",
        at=start + timedelta(minutes=4),
    )
    tracker.mark_poll("steam", start + timedelta(minutes=4))
    tracker.hide_game("200", "TBH")

    snapshot = tracker.monthly_snapshot("200")

    assert snapshot.members[0].total_seconds == 120
    assert [game.name for game in snapshot.members[0].games] == ["Visible Game"]


def test_weekly_rank_starts_monday_at_four_and_excludes_previous_week(tmp_path: Path) -> None:
    tracker = GameTimeTracker(tmp_path / "time.db", max_sample_gap=600)
    local = timezone(timedelta(hours=8))
    sunday = datetime(2026, 8, 30, 20, 0, tzinfo=local).astimezone(UTC)
    monday = datetime(2026, 8, 31, 5, 0, tzinfo=local).astimezone(UTC)
    friday = datetime(2026, 9, 4, 10, 0, tzinfo=local).astimezone(UTC)

    _observe(tracker, platform="steam", account="st", name="TestPlayer", game="Old", at=sunday)
    _observe(tracker, platform="steam", account="st", name="TestPlayer", game="Old", at=sunday + timedelta(minutes=2))
    _observe(tracker, platform="steam", account="st", name="TestPlayer", game="Weekly", at=monday)
    _observe(tracker, platform="steam", account="st", name="TestPlayer", game="Weekly", at=monday + timedelta(minutes=3))
    _observe(tracker, platform="steam", account="st", name="TestPlayer", game="Weekly", at=friday)
    _observe(tracker, platform="steam", account="st", name="TestPlayer", game="Weekly", at=friday + timedelta(minutes=4))
    tracker.mark_poll("steam", friday + timedelta(minutes=4))

    snapshot = tracker.weekly_snapshot("200")

    assert snapshot.period == "week"
    assert snapshot.day_start == datetime(2026, 8, 31, 4, 0, tzinfo=local)
    assert snapshot.members[0].total_seconds == 420
    assert [game.name for game in snapshot.members[0].games] == ["Weekly"]


def test_weekly_rank_applies_hidden_games(tmp_path: Path) -> None:
    tracker = GameTimeTracker(tmp_path / "time.db", max_sample_gap=600)
    start = datetime(2026, 9, 1, 5, 0, tzinfo=timezone(timedelta(hours=8))).astimezone(UTC)
    _observe(tracker, platform="steam", account="st", name="TestPlayer", game="Hidden", at=start)
    _observe(tracker, platform="steam", account="st", name="TestPlayer", game="Visible", at=start + timedelta(minutes=2))
    _observe(tracker, platform="steam", account="st", name="TestPlayer", game="Visible", at=start + timedelta(minutes=4))
    tracker.mark_poll("steam", start + timedelta(minutes=4))
    tracker.hide_game("200", "Hidden")

    snapshot = tracker.weekly_snapshot("200")

    assert [game.name for game in snapshot.members[0].games] == ["Visible"]


def test_dynamic_leaderboard_renderer_outputs_vertical_png(tmp_path: Path) -> None:
    local = timezone(timedelta(hours=8))
    start = datetime(2026, 8, 29, 4, 0, tzinfo=local)
    snapshot = LeaderboardSnapshot(
        day_start=start,
        last_poll=start.astimezone(UTC) + timedelta(hours=3),
        members=(
            TrackedMember(
                name="TestPlayer",
                avatar_url=None,
                total_seconds=5400,
                games=(TrackedGame("steam", "Game A", 5400, None),),
            ),
            TrackedMember(
                name="小明",
                avatar_url=None,
                total_seconds=3600,
                games=(
                    TrackedGame("switch", "Game B", 2400, None),
                    TrackedGame("ps", "Game C", 1200, None),
                ),
            ),
        ),
    )

    content = _render(snapshot, {})
    output = tmp_path / "rank.png"
    output.write_bytes(content)
    with Image.open(output) as image:
        assert image.format == "PNG"
        assert image.width == 900
        assert image.height > 600


def test_monthly_leaderboard_renderer_outputs_vertical_png(tmp_path: Path) -> None:
    local = timezone(timedelta(hours=8))
    start = datetime(2026, 9, 1, 4, 0, tzinfo=local)
    snapshot = LeaderboardSnapshot(
        day_start=start,
        last_poll=start.astimezone(UTC) + timedelta(days=1, hours=3),
        members=(
            TrackedMember(
                name="TestPlayer",
                avatar_url=None,
                total_seconds=7200,
                games=(TrackedGame("steam", "Game A", 7200, None),),
            ),
        ),
        period="month",
    )

    output = tmp_path / "monthly-rank.png"
    output.write_bytes(_render(snapshot, {}))
    with Image.open(output) as image:
        assert image.format == "PNG"
        assert image.width == 900
        assert image.height > 300


def test_weekly_leaderboard_renderer_outputs_vertical_png(tmp_path: Path) -> None:
    local = timezone(timedelta(hours=8))
    start = datetime(2026, 8, 31, 4, 0, tzinfo=local)
    snapshot = LeaderboardSnapshot(
        day_start=start,
        last_poll=start.astimezone(UTC) + timedelta(days=4, hours=3),
        members=(
            TrackedMember(
                name="TestPlayer",
                avatar_url=None,
                total_seconds=7200,
                games=(TrackedGame("steam", "Game A", 7200, None),),
            ),
        ),
        period="week",
    )

    output = tmp_path / "weekly-rank.png"
    output.write_bytes(_render(snapshot, {}))
    with Image.open(output) as image:
        assert image.format == "PNG"
        assert image.width == 900


def test_leaderboard_period_play_labels() -> None:
    assert _period_play_label("day") == "今日游玩"
    assert _period_play_label("week") == "本周游玩"
    assert _period_play_label("month") == "本月游玩"


def test_landscape_cover_fills_portrait_slot_like_activity_card() -> None:
    source = Image.new("RGB", (400, 160), "#e03030")
    source.paste(Image.new("RGB", (80, 160), "#20c060"), (0, 0))
    source.paste(Image.new("RGB", (80, 160), "#2060e0"), (320, 0))
    encoded = BytesIO()
    source.save(encoded, format="PNG")

    cover = _leaderboard_cover(encoded.getvalue(), (92, 125))

    assert cover.size == (92, 125)
    # The narrow portrait slot is fully filled by a centre crop, so the coloured
    # outer bands from the wide source are removed just like the activity card.
    for point in ((0, 0), (91, 0), (0, 124), (91, 124)):
        red, green, blue = cover.getpixel(point)
        assert red > green and red > blue


def test_day_start_before_0400_uses_previous_date() -> None:
    local = timezone(timedelta(hours=8))
    value = datetime(2026, 8, 29, 3, 31, tzinfo=local)
    assert day_start(value).isoformat().startswith("2026-08-28T04:00:00")


def test_month_start_uses_gaming_day_before_0400() -> None:
    local = timezone(timedelta(hours=8))
    value = datetime(2026, 9, 1, 3, 31, tzinfo=local)
    assert month_start(value).isoformat().startswith("2026-08-01T04:00:00")


def test_requested_later_month_resolves_to_previous_year(tmp_path: Path) -> None:
    tracker = GameTimeTracker(tmp_path / "time.db")
    local = timezone(timedelta(hours=8))
    reference = datetime(2026, 9, 2, 12, 0, tzinfo=local)
    snapshot = tracker.monthly_snapshot("200", at=reference, month=12)
    assert snapshot.day_start == datetime(2025, 12, 1, 4, 0, tzinfo=local)

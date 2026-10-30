import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from qq_bot.personal_rank_data import build_personal_rank_data, personal_rank_month_end


@dataclass(frozen=True)
class Game:
    platform: str
    name: str
    seconds: float
    image_url: str | None


@dataclass(frozen=True)
class Member:
    name: str
    avatar_url: str | None
    total_seconds: float
    games: tuple[Game, ...]


@dataclass(frozen=True)
class Snapshot:
    members: tuple[Member, ...]


class FakeTracker:
    def __init__(self, path: Path, snapshots: dict[str, Snapshot]):
        self.path = path
        self.snapshots = snapshots
        self.calls = []

    def personal_snapshot(self, group_id, member_name, *, period, at):
        self.calls.append((group_id, member_name, period, at))
        return self.snapshots[period]


def _tracker_db(path: Path):
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE IF EXISTS game_time_accounts")
        db.execute("""CREATE TABLE game_time_accounts (
            platform TEXT, account_id TEXT, group_id TEXT, identity_key TEXT
        )""")
        db.execute(
            "INSERT INTO game_time_accounts VALUES ('steam','steam-1','123','alice')"
        )


def _achievement_db(path: Path, *, complete: bool = True, baseline: bool = True):
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE IF EXISTS progress")
        db.execute("DROP TABLE IF EXISTS events")
        db.execute("CREATE TABLE progress (sid TEXT, app TEXT, complete INTEGER)")
        db.execute("CREATE TABLE events (sid TEXT, app TEXT, payload TEXT)")
        if complete:
            completions = [
                ("10", "Test Game", datetime(2026, 9, 5, tzinfo=timezone.utc)),
                ("11", "Old Month", datetime(2026, 8, 20, tzinfo=timezone.utc)),
                ("12", "Unplayed This Month", datetime(2026, 9, 20, tzinfo=timezone.utc)),
            ]
            for app, title, achieved_at in completions:
                db.execute("INSERT INTO progress VALUES ('steam-1',?,1)", (app,))
                db.execute(
                    "INSERT INTO events VALUES ('steam-1',?,?)",
                    (app, json.dumps({"title": title, "achieved_at": achieved_at.timestamp()})),
                )
        elif baseline:
            db.execute("INSERT INTO progress VALUES ('steam-1','20',0)")


def _fake_tracker(path: Path, month_games, week_games):
    name_member = Member("Alice", "https://img/avatar.jpg", 9876.0, tuple(month_games))
    day_member = Member("Alice", "https://img/avatar.jpg", 120.0, ())
    week_member = Member("Alice", "https://img/avatar.jpg", 3600.0, tuple(week_games))
    return FakeTracker(path, {
        "day": Snapshot((day_member,)),
        "week": Snapshot((week_member,)),
        "month": Snapshot((name_member,)),
    })


@pytest.fixture
def workspace_tmp_path(request, monkeypatch):
    path = Path(__file__).resolve().parents[1] / ".tmp" / "personal-rank-tests" / (request.node.name + '-' + uuid.uuid4().hex[:8])
    path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("STEAM_ACHIEVEMENT_DB_PATH", str(path / "steam-achievements.db"))
    monkeypatch.setenv("STEAM_DB_PATH", str(path / "steam-registry.db"))
    monkeypatch.setenv("PS5_DB_PATH", str(path / "ps5-registry.db"))
    return path


def test_builds_period_totals_and_month_badges_from_readonly_local_stores(workspace_tmp_path, monkeypatch):
    tmp_path = workspace_tmp_path
    tracker_path = tmp_path / "game-time.db"
    achievement_path = tmp_path / "steam-achievements.db"
    _tracker_db(tracker_path)
    _achievement_db(achievement_path)

    monthly = [
        Game("steam", "Test-Game", 5000, "https://cdn/steam/apps/10/header.jpg"),
        Game("steam", "Old Month", 1800, None),
        Game("ps", "Other Game", 2000, "https://cdn/other.jpg"),
    ]
    weekly = [Game("steam", "Test Game", 1200, None)]
    tracker = _fake_tracker(tracker_path, monthly, weekly)
    now = datetime(2026, 9, 25, 12, tzinfo=timezone.utc)

    result = build_personal_rank_data(tracker, "123", "Alice", now=now)

    assert (result.day_seconds, result.week_seconds, result.month_seconds) == (120, 3600, 9876)
    assert result.games == tuple(monthly)
    assert result.recent_games == tuple(weekly)
    assert result.name == "Alice"
    assert result.avatar_url == "https://img/avatar.jpg"
    assert result.as_of == now
    assert result.achievements == {
        ("steam", "Test-Game"): "全成就",
        ("steam", "Old Month"): "全成就",
        ("steam", "Test Game"): "全成就",
    }
    # Old Month was played this month but unlocked earlier; Unplayed This Month
    # was completed during the month and still counts without current playtime.
    assert result.perfect_count == 2
    assert result.platinum_count is None
    assert [call[2] for call in tracker.calls] == ["day", "week", "month"]
    assert all(call[3] == now for call in tracker.calls)


def test_missing_steam_store_is_unknown_when_month_contains_steam_game(workspace_tmp_path, monkeypatch):
    tmp_path = workspace_tmp_path
    tracker_path = tmp_path / "game-time.db"
    _tracker_db(tracker_path)
    missing_store = tmp_path / "missing-achievements.db"
    monkeypatch.setenv("STEAM_ACHIEVEMENT_DB_PATH", str(missing_store))
    game = Game("steam", "Test Game", 60, None)

    result = build_personal_rank_data(
        _fake_tracker(tracker_path, [game], []), "123", "Alice",
        now=datetime(2026, 9, 25, tzinfo=timezone.utc),
    )

    assert result.perfect_count is None
    assert result.achievements == {}
    assert result.platinum_count is None
    assert not missing_store.exists()


def test_available_store_with_no_completed_games_reports_zero(workspace_tmp_path, monkeypatch):
    tmp_path = workspace_tmp_path
    tracker_path = tmp_path / "game-time.db"
    achievement_path = tmp_path / "steam-achievements.db"
    _tracker_db(tracker_path)
    _achievement_db(achievement_path, complete=False)
    game = Game("steam", "Test Game", 60, None)

    result = build_personal_rank_data(
        _fake_tracker(tracker_path, [game], []), "123", "Alice",
        now=datetime(2026, 9, 25, tzinfo=timezone.utc),
    )

    assert result.perfect_count == 0
    assert result.achievements == {}


def test_store_without_progress_baseline_is_unknown_for_steam_players(workspace_tmp_path, monkeypatch):
    tmp_path = workspace_tmp_path
    tracker_path = tmp_path / "game-time.db"
    achievement_path = tmp_path / "steam-achievements.db"
    _tracker_db(tracker_path)
    _achievement_db(achievement_path, complete=False, baseline=False)
    game = Game("steam", "Test Game", 60, None)

    result = build_personal_rank_data(
        _fake_tracker(tracker_path, [game], []), "123", "Alice",
        now=datetime(2026, 9, 25, tzinfo=timezone.utc),
    )

    assert result.perfect_count is None
    assert result.achievements == {}


def test_month_award_counts_when_current_progress_is_incomplete(workspace_tmp_path):
    tracker_path = workspace_tmp_path / "game-time.db"
    achievement_path = workspace_tmp_path / "steam-achievements.db"
    _tracker_db(tracker_path)
    with sqlite3.connect(achievement_path) as db:
        db.execute("CREATE TABLE progress (sid TEXT, app TEXT, complete INTEGER)")
        db.execute("CREATE TABLE events (sid TEXT, app TEXT, payload TEXT)")
        db.execute("INSERT INTO progress VALUES ('steam-1','30',0)")
        db.execute(
            "INSERT INTO events VALUES ('steam-1','30',?)",
            (json.dumps({
                "title": "Recently Reset Game",
                "achieved_at": datetime(2026, 9, 10, tzinfo=timezone.utc).timestamp(),
            }),),
        )
    game = Game("steam", "Recently Reset Game", 60, None)

    result = build_personal_rank_data(
        _fake_tracker(tracker_path, [game], []), "123", "Alice",
        now=datetime(2026, 9, 25, tzinfo=timezone.utc),
    )

    assert result.perfect_count == 1
    assert result.achievements == {}


def test_undated_completion_makes_count_unknown_but_keeps_current_badge(workspace_tmp_path):
    tracker_path = workspace_tmp_path / "game-time.db"
    achievement_path = workspace_tmp_path / "steam-achievements.db"
    _tracker_db(tracker_path)
    with sqlite3.connect(achievement_path) as db:
        db.execute("CREATE TABLE progress (sid TEXT, app TEXT, complete INTEGER)")
        db.execute("CREATE TABLE events (sid TEXT, app TEXT, payload TEXT)")
        db.execute("INSERT INTO progress VALUES ('steam-1','40',1)")
        db.execute(
            "INSERT INTO events VALUES ('steam-1','40',?)",
            (json.dumps({"title": "Unknown Date Game"}),),
        )
    game = Game("steam", "Unknown Date Game", 60, None)

    result = build_personal_rank_data(
        _fake_tracker(tracker_path, [game], []), "123", "Alice",
        now=datetime(2026, 9, 25, tzinfo=timezone.utc),
    )

    assert result.perfect_count is None
    assert result.achievements == {("steam", "Unknown Date Game"): "全成就"}


def test_no_month_snapshot_keeps_requested_name_and_zero_periods(workspace_tmp_path):
    tracker = FakeTracker(workspace_tmp_path / "absent.db", {
        "day": Snapshot(()), "week": Snapshot(()), "month": Snapshot(()),
    })

    result = build_personal_rank_data(
        tracker, "123", "Nobody", now=datetime(2026, 9, 25, tzinfo=timezone.utc),
    )

    assert result.name == "Nobody"
    assert result.avatar_url is None
    assert result.day_seconds == result.week_seconds == result.month_seconds == 0
    assert result.games == result.recent_games == ()
    assert result.perfect_count == 0
    assert result.platinum_count is None


@pytest.mark.parametrize('now,month,year', [
    (datetime(2026, 10, 4, tzinfo=timezone.utc), 9, 2026),
    (datetime(2026, 1, 15, tzinfo=timezone.utc), 12, 2025),
    (datetime(2026, 10, 1, 3, 59, tzinfo=timezone(timedelta(hours=8))), 8, 2026),
    (datetime(2026, 10, 1, 4, tzinfo=timezone(timedelta(hours=8))), 9, 2026),
])
def test_final_month_uses_gaming_boundary_and_previous_year(now, month, year):
    end = personal_rank_month_end(month, now=now)
    assert (end + timedelta(microseconds=1)) == datetime(
        year + (month == 12), month % 12 + 1, 1, 4, tzinfo=timezone(timedelta(hours=8)),
    )


@pytest.mark.parametrize('now,month', [
    (datetime(2026, 10, 4, tzinfo=timezone.utc), 10),
    (datetime(2026, 10, 1, 3, 59, tzinfo=timezone(timedelta(hours=8))), 9),
])
def test_unfinished_month_is_rejected(now, month):
    with pytest.raises(ValueError, match='尚未结束'):
        personal_rank_month_end(month, now=now)


def test_final_month_totals_exclude_next_month_records(workspace_tmp_path):
    from qq_bot.game_time_tracker import GameTimeTracker
    local = timezone(timedelta(hours=8))
    tracker = GameTimeTracker(workspace_tmp_path / 'time.db')
    before = datetime(2026, 10, 1, 3, 59, tzinfo=local)
    after = before + timedelta(minutes=2)
    for moment in (before, after):
        tracker.observe(platform='switch', account_id='a', group_id='123', display_name='Alice',
                        avatar_url=None, game_name='Game', game_key='game', game_image_url=None,
                        observed_at=moment)
    result = build_personal_rank_data(tracker, '123', 'Alice',
        now=personal_rank_month_end(9, now=after), finalized_month=True)
    assert result.day_seconds == result.week_seconds == result.month_seconds == 60
    assert result.games[0].seconds == 60
    assert result.finalized_month


def test_historical_steam_awards_ignore_later_unlocks_and_current_progress(workspace_tmp_path):
    tracker_path = workspace_tmp_path / 'game-time.db'
    _tracker_db(tracker_path)
    _achievement_db(workspace_tmp_path / 'steam-achievements.db')
    with sqlite3.connect(workspace_tmp_path / 'steam-achievements.db') as db:
        db.execute("UPDATE progress SET complete=0 WHERE app='10'")
        db.execute("INSERT INTO progress VALUES ('steam-1','99',1)")
        db.execute("INSERT INTO events VALUES ('steam-1','99',?)", (json.dumps({
            'title': 'Future Game', 'achieved_at': datetime(2026, 10, 2, tzinfo=timezone.utc).timestamp(),
        }),))
    games = [Game('steam', name, 60, None) for name in ('Test Game', 'Old Month', 'Future Game')]
    result = build_personal_rank_data(_fake_tracker(tracker_path, games, []), '123', 'Alice',
        now=personal_rank_month_end(9, now=datetime(2026, 10, 4, tzinfo=timezone.utc)), finalized_month=True)
    assert result.perfect_count == 2
    assert result.achievements == {('steam', 'Test Game'): '全成就', ('steam', 'Old Month'): '全成就'}

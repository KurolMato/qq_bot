from __future__ import annotations

import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "game_time.db"
LOCAL_TZ = timezone(timedelta(hours=8), "Asia/Hong_Kong")


@dataclass(frozen=True)
class TrackedGame:
    platform: str
    name: str
    seconds: float
    image_url: str | None


@dataclass(frozen=True)
class TrackedMember:
    name: str
    avatar_url: str | None
    total_seconds: float
    games: tuple[TrackedGame, ...]


@dataclass(frozen=True)
class LeaderboardSnapshot:
    day_start: datetime
    last_poll: datetime | None
    members: tuple[TrackedMember, ...]
    period: str = "day"


def _utc(value: datetime | None = None) -> datetime:
    result = value or datetime.now(timezone.utc)
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _parse_time(value: str) -> datetime:
    return _utc(datetime.fromisoformat(value))


def day_start(value: datetime) -> datetime:
    local = _utc(value).astimezone(LOCAL_TZ)
    date = local.date() if local.hour >= 4 else (local - timedelta(days=1)).date()
    return datetime(date.year, date.month, date.day, 4, tzinfo=LOCAL_TZ)


def month_start(value: datetime) -> datetime:
    gaming_day = day_start(value)
    return datetime(gaming_day.year, gaming_day.month, 1, 4, tzinfo=LOCAL_TZ)


def week_start(value: datetime) -> datetime:
    gaming_day = day_start(value)
    return gaming_day - timedelta(days=gaming_day.weekday())


def _identity_key(value: str) -> str:
    return " ".join(value.strip().split()).casefold()


def _game_name_key(value: str) -> str:
    """Normalize a command/display game name for per-group hiding."""
    return "".join(character for character in value.casefold() if character.isalnum())


def _game_name_matches(hidden_key: str, game_key: str) -> bool:
    """Return whether a hidden query fuzzily matches a displayed game name."""
    if not hidden_key or not game_key:
        return False
    if hidden_key == game_key:
        return True
    # Avoid a one/two-letter query hiding unrelated games, while allowing
    # abbreviations such as “TBH” to match “TBH 塔斯巴克英雄”.
    return len(hidden_key) >= 3 and hidden_key in game_key


class GameTimeTracker:
    def __init__(self, path: Path = DEFAULT_DB_PATH, *, max_sample_gap: float | None = None) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_sample_gap = max(
            float(max_sample_gap if max_sample_gap is not None else os.getenv("GAME_TIME_MAX_SAMPLE_GAP", "300")),
            30.0,
        )
        self._lock = threading.RLock()
        self._initialise()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _initialise(self) -> None:
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS game_time_accounts (
                    platform TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    group_id TEXT NOT NULL,
                    identity_key TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    avatar_url TEXT,
                    game_key TEXT,
                    game_name TEXT,
                    game_image_url TEXT,
                    observed_at TEXT NOT NULL,
                    PRIMARY KEY (platform, account_id, group_id)
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS game_time_buckets (
                    day_key TEXT NOT NULL,
                    group_id TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    game_key TEXT NOT NULL,
                    game_name TEXT NOT NULL,
                    game_image_url TEXT,
                    seconds REAL NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (day_key, group_id, platform, account_id, game_key)
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS game_time_polls (
                    platform TEXT PRIMARY KEY,
                    observed_at TEXT NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS game_time_hidden_games (
                    group_id TEXT NOT NULL,
                    game_key TEXT NOT NULL,
                    game_name TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (group_id, game_key)
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS game_time_other_rank_members (
                    group_id TEXT NOT NULL,
                    identity_key TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (group_id, identity_key)
                )
                """
            )

            db.execute(
                """CREATE TABLE IF NOT EXISTS game_time_qq_bindings (
                    group_id TEXT NOT NULL, qq_user_id TEXT NOT NULL,
                    identity_key TEXT NOT NULL, display_name TEXT NOT NULL,
                    PRIMARY KEY (group_id, qq_user_id)
                )"""
            )

    def bind_qq_member(self, group_id: str, user_id: str, name: str) -> str:
        name = " ".join(name.split())
        if not name:
            raise ValueError("用法：/绑定 昵称或玩家名")
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT INTO game_time_qq_bindings VALUES (?, ?, ?, ?)
                   ON CONFLICT(group_id, qq_user_id) DO UPDATE SET
                   identity_key=excluded.identity_key, display_name=excluded.display_name""",
                (str(group_id), str(user_id), _identity_key(name), name),
            )
        return name

    def bound_member(self, group_id: str, user_id: str) -> str | None:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT display_name FROM game_time_qq_bindings WHERE group_id=? AND qq_user_id=?",
                (str(group_id), str(user_id)),
            ).fetchone()
        return str(row[0]) if row else None

    def _resolve_member(self, db: sqlite3.Connection, group_id: str, member_name: str) -> tuple[str, str]:
        query = _identity_key(member_name)
        if not query:
            raise ValueError("成员名不能为空")
        rows = db.execute(
            """
            SELECT identity_key, MAX(display_name) AS display_name
            FROM game_time_accounts
            WHERE group_id = ?
            GROUP BY identity_key
            """,
            (group_id,),
        ).fetchall()
        exact = [row for row in rows if str(row["identity_key"]) == query]
        if exact:
            row = exact[0]
            return str(row["identity_key"]), str(row["display_name"])
        fuzzy = [row for row in rows if query in str(row["identity_key"])]
        if len(fuzzy) == 1:
            row = fuzzy[0]
            return str(row["identity_key"]), str(row["display_name"])
        if len(fuzzy) > 1:
            names = "、".join(str(row["display_name"]) for row in fuzzy[:5])
            raise ValueError(f"匹配到多个成员：{names}，请使用完整昵称")
        raise ValueError(f"没有找到成员：{member_name}")

    def move_member_to_other_rank(self, group_id: str, member_name: str) -> tuple[str, bool]:
        group_id = str(group_id).strip()
        if not group_id:
            raise ValueError("群号不能为空")
        with self._lock, self._connect() as db:
            identity_key, display_name = self._resolve_member(db, group_id, member_name)
            cursor = db.execute(
                """
                INSERT OR IGNORE INTO game_time_other_rank_members
                    (group_id, identity_key, display_name, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (group_id, identity_key, display_name, datetime.now(timezone.utc).isoformat(timespec="seconds")),
            )
        return display_name, cursor.rowcount > 0

    def restore_member_to_main_rank(self, group_id: str, member_name: str) -> tuple[str, bool]:
        group_id = str(group_id).strip()
        if not group_id:
            raise ValueError("群号不能为空")
        with self._lock, self._connect() as db:
            identity_key, display_name = self._resolve_member(db, group_id, member_name)
            cursor = db.execute(
                "DELETE FROM game_time_other_rank_members WHERE group_id = ? AND identity_key = ?",
                (group_id, identity_key),
            )
        return display_name, cursor.rowcount > 0

    def hide_game(self, group_id: str, game_name: str) -> bool:
        group_id = str(group_id).strip()
        game_name = " ".join(str(game_name).strip().split())
        game_key = _game_name_key(game_name)
        if not group_id or not game_key:
            raise ValueError("游戏名不能为空")
        with self._lock, self._connect() as db:
            cursor = db.execute(
                """
                INSERT OR IGNORE INTO game_time_hidden_games
                    (group_id, game_key, game_name, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    group_id,
                    game_key,
                    game_name,
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                ),
            )
        return cursor.rowcount > 0

    def unhide_game(self, group_id: str, game_name: str) -> bool:
        group_id = str(group_id).strip()
        game_key = _game_name_key(game_name)
        if not group_id or not game_key:
            raise ValueError("游戏名不能为空")
        with self._lock, self._connect() as db:
            rows = db.execute(
                "SELECT game_key FROM game_time_hidden_games WHERE group_id = ?",
                (group_id,),
            ).fetchall()
            matching_keys = [
                str(row["game_key"])
                for row in rows
                if _game_name_matches(game_key, str(row["game_key"]))
                or _game_name_matches(str(row["game_key"]), game_key)
            ]
            if not matching_keys:
                return False
            db.execute(
                "DELETE FROM game_time_hidden_games WHERE group_id = ? AND game_key IN ({})".format(
                    ",".join("?" for _ in matching_keys)
                ),
                (group_id, *matching_keys),
            )
        return True

    @staticmethod
    def _segments(start: datetime, end: datetime) -> list[tuple[str, float]]:
        segments: list[tuple[str, float]] = []
        cursor = _utc(start)
        end = _utc(end)
        while cursor < end:
            bucket_start = day_start(cursor)
            next_boundary = (bucket_start + timedelta(days=1)).astimezone(timezone.utc)
            segment_end = min(end, next_boundary)
            seconds = (segment_end - cursor).total_seconds()
            if seconds > 0:
                segments.append((bucket_start.date().isoformat(), seconds))
            cursor = segment_end
        return segments

    def observe(
        self,
        *,
        platform: str,
        account_id: str,
        group_id: str,
        display_name: str,
        avatar_url: str | None,
        game_key: str | None,
        game_name: str | None,
        game_image_url: str | None,
        observed_at: datetime | None = None,
    ) -> None:
        observed = _utc(observed_at)
        platform = platform.strip().casefold()
        account_id = account_id.strip()
        group_id = group_id.strip()
        display_name = " ".join(display_name.strip().split()) or account_id
        identity_key = _identity_key(display_name)
        normalized_game_key = game_key.strip().casefold() if game_key else None
        normalized_game_name = game_name.strip() if game_name else None

        with self._lock, self._connect() as db:
            previous = db.execute(
                "SELECT * FROM game_time_accounts WHERE platform = ? AND account_id = ? AND group_id = ?",
                (platform, account_id, group_id),
            ).fetchone()
            if previous is not None and previous["game_key"] and previous["game_name"]:
                previous_at = _parse_time(previous["observed_at"])
                elapsed = (observed - previous_at).total_seconds()
                if 0 < elapsed <= self.max_sample_gap:
                    previous_image_url = previous["game_image_url"]
                    if normalized_game_key == previous["game_key"] and game_image_url:
                        previous_image_url = game_image_url
                    for day_key, seconds in self._segments(previous_at, observed):
                        db.execute(
                            """
                            INSERT INTO game_time_buckets
                            (day_key, group_id, platform, account_id, game_key, game_name,
                             game_image_url, seconds, updated_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(day_key, group_id, platform, account_id, game_key)
                            DO UPDATE SET
                                game_name = excluded.game_name,
                                game_image_url = COALESCE(excluded.game_image_url, game_time_buckets.game_image_url),
                                seconds = game_time_buckets.seconds + excluded.seconds,
                                updated_at = excluded.updated_at
                            """,
                            (
                                day_key,
                                group_id,
                                platform,
                                account_id,
                                previous["game_key"],
                                previous["game_name"],
                                previous_image_url,
                                seconds,
                                observed.isoformat(),
                            ),
                        )

            db.execute(
                """
                INSERT INTO game_time_accounts
                (platform, account_id, group_id, identity_key, display_name, avatar_url,
                 game_key, game_name, game_image_url, observed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(platform, account_id, group_id) DO UPDATE SET
                    identity_key = excluded.identity_key,
                    display_name = excluded.display_name,
                    avatar_url = COALESCE(excluded.avatar_url, game_time_accounts.avatar_url),
                    game_key = excluded.game_key,
                    game_name = excluded.game_name,
                    game_image_url = excluded.game_image_url,
                    observed_at = excluded.observed_at
                """,
                (
                    platform,
                    account_id,
                    group_id,
                    identity_key,
                    display_name,
                    avatar_url,
                    normalized_game_key,
                    normalized_game_name,
                    game_image_url,
                    observed.isoformat(),
                ),
            )

            # A platform may only resolve a cover after the first activity sample.
            # Backfill today's already-created bucket immediately so the next
            # leaderboard render does not have to wait for another poll.
            if normalized_game_key and game_image_url:
                current_day_key = day_start(observed).date().isoformat()
                db.execute(
                    """
                    UPDATE game_time_buckets
                    SET game_image_url = ?, updated_at = ?
                    WHERE day_key = ? AND group_id = ? AND platform = ?
                      AND account_id = ? AND game_key = ?
                    """,
                    (
                        game_image_url,
                        observed.isoformat(),
                        current_day_key,
                        group_id,
                        platform,
                        account_id,
                        normalized_game_key,
                    ),
                )

    def mark_poll(self, platform: str, observed_at: datetime | None = None) -> None:
        observed = _utc(observed_at)
        with self._lock, self._connect() as db:
            db.execute(
                """
                INSERT INTO game_time_polls (platform, observed_at) VALUES (?, ?)
                ON CONFLICT(platform) DO UPDATE SET observed_at = excluded.observed_at
                """,
                (platform.strip().casefold(), observed.isoformat()),
            )

    def snapshot(
        self,
        group_id: str,
        at: datetime | None = None,
        *,
        board: str = "main",
    ) -> LeaderboardSnapshot:
        return self._snapshot(group_id, at=at, period="day", board=board)

    def group_ids(self) -> list[str]:
        with self._connect() as db:
            return [row[0] for row in db.execute(
                "SELECT group_id FROM game_time_accounts UNION SELECT group_id FROM game_time_buckets"
            )]

    def monthly_snapshot(
        self,
        group_id: str,
        at: datetime | None = None,
        *,
        month: int | None = None,
        board: str = "main",
    ) -> LeaderboardSnapshot:
        if month is None:
            return self._snapshot(group_id, at=at, period="month", board=board)
        if not 1 <= month <= 12:
            raise ValueError("月份必须为 1 到 12")

        reference = _utc(at)
        current_month = month_start(reference)
        year = current_month.year if month <= current_month.month else current_month.year - 1
        start = datetime(year, month, 1, 4, tzinfo=LOCAL_TZ)
        if month == 12:
            next_month = datetime(year + 1, 1, 1, 4, tzinfo=LOCAL_TZ)
        else:
            next_month = datetime(year, month + 1, 1, 4, tzinfo=LOCAL_TZ)
        end = next_month - timedelta(days=1)
        return self._snapshot(
            group_id,
            at=at,
            period="month",
            range_start=start,
            range_end=end,
            board=board,
        )

    def yesterday_snapshot(
        self, group_id: str, at: datetime | None = None, *, board: str = "main"
    ) -> LeaderboardSnapshot:
        reference = _utc(at)
        start = day_start(reference) - timedelta(days=1)
        return self._snapshot(
            group_id, at=reference, period="yesterday", range_start=start,
            range_end=start, board=board,
        )

    def weekly_snapshot(
        self,
        group_id: str,
        at: datetime | None = None,
        *,
        board: str = "main",
    ) -> LeaderboardSnapshot:
        return self._snapshot(group_id, at=at, period="week", board=board)

    def personal_snapshot(
        self, group_id: str, member_name: str, *, period: str = "day",
        at: datetime | None = None,
    ) -> LeaderboardSnapshot:
        with self._lock, self._connect() as db:
            identity, _ = self._resolve_member(db, str(group_id), member_name)
        reference = _utc(at)
        if period == "yesterday":
            snapshot = self.yesterday_snapshot(group_id, at=reference, board="all")
        elif period in {"day", "week", "month"}:
            snapshot = self._snapshot(group_id, at=reference, period=period, board="all")
        else:
            raise ValueError("时间范围请使用 d、y、w 或 m")
        return LeaderboardSnapshot(
            snapshot.day_start, snapshot.last_poll,
            tuple(m for m in snapshot.members if _identity_key(m.name) == identity),
            snapshot.period,
        )

    def _snapshot(
        self,
        group_id: str,
        *,
        at: datetime | None,
        period: str,
        range_start: datetime | None = None,
        range_end: datetime | None = None,
        board: str = "main",
    ) -> LeaderboardSnapshot:
        if board not in {"main", "other", "all"}:
            raise ValueError("未知排行榜分组")
        with self._lock, self._connect() as db:
            hidden_games = {
                str(row["game_key"])
                for row in db.execute(
                    "SELECT game_key FROM game_time_hidden_games WHERE group_id = ?",
                    (str(group_id),),
                ).fetchall()
            }
            other_members = {
                str(row["identity_key"])
                for row in db.execute(
                    "SELECT identity_key FROM game_time_other_rank_members WHERE group_id = ?",
                    (str(group_id),),
                ).fetchall()
            }
            poll_row = db.execute("SELECT MAX(observed_at) AS value FROM game_time_polls").fetchone()
            last_poll = _parse_time(poll_row["value"]) if poll_row and poll_row["value"] else None
            reference = _utc(at) if at is not None else (last_poll or _utc())
            current_day = day_start(reference)
            end = range_end or current_day
            if range_start is not None:
                start = range_start
            elif period == "month":
                start = month_start(reference)
            elif period == "week":
                start = week_start(reference)
            else:
                start = current_day
            rows = db.execute(
                """
                SELECT b.*, a.identity_key, a.display_name, a.avatar_url
                FROM game_time_buckets b
                JOIN game_time_accounts a
                  ON a.platform = b.platform
                 AND a.account_id = b.account_id
                 AND a.group_id = b.group_id
                WHERE b.day_key BETWEEN ? AND ?
                  AND b.group_id = ? AND b.seconds > 0
                """,
                (start.date().isoformat(), end.date().isoformat(), str(group_id)),
            ).fetchall()

            if range_start is not None and end.date() < current_day.date():
                historical_poll = max(
                    (_parse_time(str(row["updated_at"])) for row in rows),
                    default=None,
                )
                last_poll = historical_poll
                if period == "yesterday" and last_poll is not None:
                    # A sample crossing 04:00 belongs to both days; its write
                    # timestamp must not extend yesterday past settlement.
                    last_poll = min(last_poll, (start + timedelta(days=1)).astimezone(timezone.utc))

        members: dict[str, dict[str, object]] = {}
        for row in rows:
            is_other_member = str(row["identity_key"]) in other_members
            if board != "all" and (board == "other") != is_other_member:
                continue
            game_key = _game_name_key(str(row["game_name"]))
            if any(_game_name_matches(hidden, game_key) for hidden in hidden_games):
                continue
            member = members.setdefault(
                row["identity_key"],
                {
                    "name": row["display_name"],
                    "games": {},
                    "accounts": {},
                },
            )
            games = member["games"]
            game_id = (row["platform"], row["game_key"])
            game = games.setdefault(
                game_id,
                {
                    "platform": row["platform"],
                    "name": row["game_name"],
                    "seconds": 0.0,
                    "image_url": row["game_image_url"],
                },
            )
            game["seconds"] += float(row["seconds"])
            if row["game_image_url"]:
                game["image_url"] = row["game_image_url"]
            account_id = (row["platform"], row["account_id"])
            accounts = member["accounts"]
            account = accounts.setdefault(account_id, {"seconds": 0.0, "avatar_url": row["avatar_url"]})
            account["seconds"] += float(row["seconds"])
            if row["avatar_url"]:
                account["avatar_url"] = row["avatar_url"]

        result: list[TrackedMember] = []
        for member in members.values():
            games = sorted(member["games"].values(), key=lambda item: (-item["seconds"], item["name"]))
            accounts = sorted(member["accounts"].values(), key=lambda item: -item["seconds"])
            tracked_games = tuple(
                TrackedGame(
                    platform=str(game["platform"]),
                    name=str(game["name"]),
                    seconds=float(game["seconds"]),
                    image_url=game["image_url"],
                )
                for game in games
            )
            result.append(
                TrackedMember(
                    name=str(member["name"]),
                    avatar_url=accounts[0]["avatar_url"] if accounts else None,
                    total_seconds=sum(game.seconds for game in tracked_games),
                    games=tracked_games,
                )
            )
        result.sort(key=lambda member: (-member.total_seconds, member.name.casefold()))
        return LeaderboardSnapshot(start, last_poll, tuple(result), period)


tracker = GameTimeTracker(Path(os.getenv("GAME_TIME_DB_PATH", DEFAULT_DB_PATH)))

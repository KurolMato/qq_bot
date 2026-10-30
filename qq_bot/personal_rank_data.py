"""Read-only data assembly for the ``/rank me`` profile card."""
from __future__ import annotations

import json
import math
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .game_time_tracker import TrackedGame


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STEAM_DB_PATH = PROJECT_ROOT / "data" / "steam_registry.db"
DEFAULT_PS5_DB_PATH = PROJECT_ROOT / "data" / "ps5_registry.db"
DEFAULT_STEAM_ACHIEVEMENT_DB_PATH = PROJECT_ROOT / "data" / "steam-achievements.db"


@dataclass(frozen=True)
class PersonalRankData:
    name: str
    avatar_url: str | None
    day_seconds: float
    week_seconds: float
    month_seconds: float
    games: tuple[TrackedGame, ...]
    recent_games: tuple[TrackedGame, ...]
    platinum_count: int | None
    perfect_count: int | None
    achievements: dict[tuple[str, str], str]
    as_of: datetime
    finalized_month: bool = False


def personal_rank_month_end(month: int, *, now: datetime | None = None) -> datetime:
    """Resolve a completed gaming month, matching /rank m's year selection."""
    from .game_time_tracker import LOCAL_TZ, month_start

    if not 1 <= month <= 12:
        raise ValueError("月份必须为 1 到 12，例如 /rank me 9")
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    current = month_start(reference)
    year = current.year if month <= current.month else current.year - 1
    end = datetime(year + (month == 12), month % 12 + 1, 1, 4, tzinfo=LOCAL_TZ)
    if reference < end:
        raise ValueError(f"{year}年{month}月尚未结束，请发送 /rank me 查看当前个人档案。")
    return end - timedelta(microseconds=1)


def _normal_name(value: object) -> str:
    return " ".join(str(value or "").strip().split()).casefold()


def _normal_title(value: object) -> str:
    return "".join(character for character in str(value or "").casefold() if character.isalnum())


def _readonly_connect(path: Path) -> sqlite3.Connection | None:
    """Open an existing SQLite database in read-only mode, without creating it."""
    try:
        resolved = path.expanduser().resolve()
        if not resolved.is_file():
            return None
        db = sqlite3.connect(resolved.as_uri() + "?mode=ro", uri=True, timeout=2)
        db.row_factory = sqlite3.Row
        return db
    except (OSError, sqlite3.Error, ValueError):
        return None


def _table_columns(db: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(row["name"]) for row in db.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _path_from_env(variable: str, fallback: Path) -> Path:
    value = os.getenv(variable, "").strip()
    if not value:
        return fallback
    configured = Path(value).expanduser()
    return configured if configured.is_absolute() else PROJECT_ROOT / configured


def _tracker_accounts(tracker: Any, group_id: str, member_name: str, platform: str) -> set[str]:
    """Resolve account ids from the tracker's own group-scoped identity rows."""
    path = getattr(tracker, "path", None)
    if path is None:
        return set()
    db = _readonly_connect(Path(path))
    if db is None:
        return set()
    try:
        rows = db.execute(
            "SELECT account_id FROM game_time_accounts "
            "WHERE group_id=? AND platform=? AND identity_key=?",
            (str(group_id), platform.casefold(), _normal_name(member_name)),
        ).fetchall()
        return {str(row["account_id"]) for row in rows if row["account_id"]}
    except sqlite3.Error:
        return set()
    finally:
        db.close()


def _registry_account_ids(path: Path, table: str, group_id: str, member_names: set[str],
                          *, account_column: str, aliases: tuple[str, ...]) -> set[str]:
    db = _readonly_connect(path)
    if db is None:
        return set()
    try:
        columns = _table_columns(db, table)
        needed = {account_column, "group_id", *aliases}
        if not needed.issubset(columns):
            return set()
        selected = ", ".join([account_column, *aliases])
        rows = db.execute(
            f"SELECT {selected} FROM {table} WHERE group_id=?",
            (str(group_id),),
        ).fetchall()
        matches = set()
        for row in rows:
            # A configured nickname replaces the platform name as the effective
            # member identity. Matching every alias could link another member
            # who happens to share the same underlying platform display name.
            effective_name = next(
                (row[column] for column in aliases if row[column] and str(row[column]).strip()),
                None,
            )
            if _normal_name(effective_name) in member_names:
                if row[account_column]:
                    matches.add(str(row[account_column]))
        return matches
    except sqlite3.Error:
        return set()
    finally:
        db.close()


def read_bound_account_ids(tracker: Any, group_id: str, member_name: str,
                           platform: str) -> set[str]:
    """Return this group's account ids for a profile name, using local stores only.

    The helper is also useful to optional platform-specific enrichment. Registry
    databases are opened read-only and never initialized or migrated here.
    """
    platform_key = platform.strip().casefold()
    account_ids = _tracker_accounts(tracker, str(group_id), member_name, platform_key)

    if platform_key in {"steam", "steam_id"}:
        path = _path_from_env("STEAM_DB_PATH", DEFAULT_STEAM_DB_PATH)
        account_ids |= _registry_account_ids(
            path, "steam_subscriptions", str(group_id), {_normal_name(member_name)},
            account_column="steam_id", aliases=("nickname", "display_name"),
        )
        return account_ids
    if platform_key in {"ps", "psn", "ps5"}:
        path = _path_from_env("PS5_DB_PATH", DEFAULT_PS5_DB_PATH)
        account_ids |= _registry_account_ids(
            path, "ps5_subscriptions", str(group_id), {_normal_name(member_name)},
            account_column="account_id", aliases=("nickname", "online_id"),
        )
        return account_ids
    return set()


def _snapshot_member(tracker: Any, group_id: str, member_name: str, period: str,
                     reference: datetime) -> Any | None:
    snapshot = tracker.personal_snapshot(group_id, member_name, period=period, at=reference)
    return snapshot.members[0] if snapshot.members else None


def _gaming_month_start(value: datetime) -> datetime:
    """Return the tracker's 04:00 Asia/Hong_Kong gaming-month boundary."""
    local_tz = timezone(timedelta(hours=8), "Asia/Hong_Kong")
    local = value.astimezone(local_tz)
    gaming_date = local.date() if local.hour >= 4 else (local - timedelta(days=1)).date()
    return datetime(gaming_date.year, gaming_date.month, 1, 4, tzinfo=local_tz)


def _steam_completion_data(account_ids: set[str], reference: datetime, *, historical: bool = False
                           ) -> tuple[set[str] | None, int | None, bool]:
    """Return current full-game titles, month awards, and baseline availability."""
    path = _path_from_env("STEAM_ACHIEVEMENT_DB_PATH", DEFAULT_STEAM_ACHIEVEMENT_DB_PATH)
    db = _readonly_connect(path)
    if db is None:
        return None, None, False
    try:
        if not {"progress", "events"}.issubset(
            {str(row["name"]) for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        ):
            return None, None, False
        if not account_ids:
            return None, None, False
        placeholders = ",".join("?" for _ in account_ids)
        baseline = db.execute(
            f"SELECT COUNT(*) AS count FROM progress WHERE sid IN ({placeholders})",
            tuple(sorted(account_ids)),
        ).fetchone()
        if not baseline or int(baseline["count"] or 0) == 0:
            return None, None, False
        rows = db.execute(
            "SELECT e.sid, e.app, e.payload, COALESCE(p.complete, 0) AS complete "
            "FROM events e LEFT JOIN progress p ON p.sid=e.sid AND p.app=e.app "
            f"WHERE e.sid IN ({placeholders})",
            tuple(sorted(account_ids)),
        ).fetchall()
        titles: set[str] = set()
        current_app_ids: set[str] = set()
        month_app_ids: set[str] = set()
        dates_complete = True
        start_timestamp = _gaming_month_start(reference).timestamp()
        end_timestamp = reference.timestamp()
        for row in rows:
            app_id = str(row["app"])
            try:
                payload = json.loads(row["payload"])
            except (TypeError, json.JSONDecodeError):
                payload = None
            title = payload.get("title") if isinstance(payload, dict) else None
            normalized = _normal_title(title)
            if bool(row["complete"]) and not historical:
                current_app_ids.add(app_id)
            if bool(row["complete"]) and normalized and not historical:
                titles.add(normalized)
            achieved_at = payload.get("achieved_at") if isinstance(payload, dict) else None
            try:
                unlocked = float(achieved_at) if not isinstance(achieved_at, bool) else math.nan
            except (TypeError, ValueError):
                unlocked = math.nan
            if not math.isfinite(unlocked) or unlocked <= 0:
                dates_complete = False
            elif unlocked > end_timestamp:
                if not historical:
                    dates_complete = False
            elif historical:
                current_app_ids.add(app_id)
                if normalized:
                    titles.add(normalized)
                if start_timestamp <= unlocked:
                    month_app_ids.add(app_id)
            elif start_timestamp <= unlocked:
                # Completion events remain valid evidence of an award in this
                # month even if later progress polling no longer says complete.
                month_app_ids.add(app_id)

        # Steam registry titles are localized separately from the completion
        # event title. Reading this cache lets the same app match its displayed
        # name without an API call.
        registry_path = _path_from_env("STEAM_DB_PATH", DEFAULT_STEAM_DB_PATH)
        registry = _readonly_connect(registry_path)
        if registry is not None:
            try:
                if "steam_localized_game_names" in {
                    str(row["name"]) for row in registry.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }:
                    for app in current_app_ids:
                        row = registry.execute(
                            "SELECT localized_name FROM steam_localized_game_names WHERE app_id=?",
                            (app,),
                        ).fetchone()
                        if row:
                            localized = _normal_title(row["localized_name"])
                            if localized:
                                titles.add(localized)
            except sqlite3.Error:
                pass
            finally:
                registry.close()
        return titles, len(month_app_ids) if dates_complete else None, True
    except sqlite3.Error:
        return None, None, False
    finally:
        db.close()


def build_personal_rank_data(tracker: Any, group_id: str, member_name: str,
                             *, now: datetime | None = None, finalized_month: bool = False) -> PersonalRankData:
    """Collect a profile card's period totals and current-month achievement state.

    Period totals and game lists come from the tracker's canonical day/week/month
    snapshots, so its 04:00 gaming-day and month boundaries remain authoritative.
    All achievement reads are local and read-only; missing trophy data stays None.
    """
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)

    day_member = _snapshot_member(tracker, str(group_id), member_name, "day", reference)
    week_member = _snapshot_member(tracker, str(group_id), member_name, "week", reference)
    month_member = _snapshot_member(tracker, str(group_id), member_name, "month", reference)

    preferred = month_member or week_member or day_member
    games = tuple(month_member.games) if month_member else ()
    recent_games = tuple(week_member.games) if week_member else ()
    steam_games = [game for game in games if game.platform.casefold() == "steam"]

    achievements: dict[tuple[str, str], str] = {}
    bound_name = preferred.name if preferred else member_name
    steam_ids = read_bound_account_ids(tracker, str(group_id), bound_name, "steam")
    completion_titles, perfect_count, store_available = _steam_completion_data(
        steam_ids, reference, historical=finalized_month,
    )
    if not steam_ids:
        # With no Steam games in the month's playtime there is nothing to count.
        # If a Steam title was played, unavailable account linkage stays unknown.
        perfect_count = 0 if not steam_games else None
    if store_available and completion_titles is not None:
        for game in (*games, *recent_games):
            if game.platform.casefold() == "steam" and _normal_title(game.name) in completion_titles:
                achievements[(game.platform.casefold(), game.name)] = "全成就"

    # The local trophy reader fills this after time-data assembly. Missing or
    # unfinished PS baselines remain unknown; never infer zero from playtime.
    return PersonalRankData(
        name=preferred.name if preferred else member_name,
        avatar_url=(preferred.avatar_url if preferred else None),
        day_seconds=day_member.total_seconds if day_member else 0.0,
        week_seconds=week_member.total_seconds if week_member else 0.0,
        month_seconds=month_member.total_seconds if month_member else 0.0,
        games=games,
        recent_games=recent_games,
        platinum_count=None,
        perfect_count=perfect_count,
        achievements=achievements,
        as_of=reference,
        finalized_month=finalized_month,
    )

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
from io import BytesIO
from pathlib import Path
from typing import Any, Mapping

from nonebot.adapters.onebot.v11 import Message, MessageSegment
from PIL import Image, ImageDraw, ImageFilter, ImageOps

from .list_cards import ListCardEntry, _load_assets, _paste_rounded, _render_fallback_mark
from .steam_registry import SteamClient
from .switch_presence import _fitted_text, _font


logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATABASE_PATH = PROJECT_ROOT / "data" / "game-calendar.db"
LOCAL_IMAGE_DIR = PROJECT_ROOT / "data" / "game-calendar-images"
STORE_SEARCH_URL = "https://store.steampowered.com/api/storesearch/"
STORE_APP_URL = "https://store.steampowered.com/app/{app_id}/"
CHINA_TIMEZONE = timezone(timedelta(hours=8))
CALENDAR_DUPLICATE_DAY_WINDOW = 7


class GameLookupError(RuntimeError):
    pass


@dataclass(frozen=True)
class CalendarGame:
    app_id: str
    name: str
    release_date: date
    image_url: str | None
    platform: str = "Steam"


def _comparable_game_name(value: str) -> str:
    # PS Store commonly appends language lists such as “(泰语, 日语, ...)”.
    # They describe the product page rather than a different game.
    without_notes = re.sub(r"[（(\[【].*?[）)\]】]", "", value)
    return _normal_name(without_notes)


def game_names_highly_similar(left: str, right: str) -> bool:
    left_without_notes = re.sub(r"[（(\[【].*?[）)\]】]", "", left)
    right_without_notes = re.sub(r"[（(\[【].*?[）)\]】]", "", right)
    left_markers = (
        re.findall(r"\d+", left_without_notes),
        re.findall(r"\b[IVXLCDM]{2,}\b", left_without_notes),
    )
    right_markers = (
        re.findall(r"\d+", right_without_notes),
        re.findall(r"\b[IVXLCDM]{2,}\b", right_without_notes),
    )
    # High textual similarity must not collapse sequels such as Nioh 2/3 or
    # entries whose Roman-numeral installment numbers differ.
    if left_markers != right_markers:
        return False
    first = _comparable_game_name(left)
    second = _comparable_game_name(right)
    if first == second:
        return bool(first)
    if min(len(first), len(second)) < 4:
        return False
    similarity = SequenceMatcher(None, first, second).ratio()
    containment = first in second or second in first
    length_ratio = min(len(first), len(second)) / max(len(first), len(second))
    return similarity >= 0.82 or (containment and length_ratio >= 0.62)


def _platform_priority(value: str) -> int:
    priorities = {"steam": 30, "ps": 20, "psn": 20, "switch": 10}
    return priorities.get(value.strip().casefold(), 0)


class GameCalendarRegistry:
    def __init__(self, path: Path = DATABASE_PATH) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS game_releases (
                    app_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    release_date TEXT NOT NULL,
                    image_url TEXT,
                    added_by TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(game_releases)").fetchall()
            }
            if "platform" not in columns:
                connection.execute(
                    "ALTER TABLE game_releases ADD COLUMN platform TEXT NOT NULL DEFAULT 'Steam'"
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS game_release_notifications (
                    group_id TEXT NOT NULL,
                    release_date TEXT NOT NULL,
                    lead_days INTEGER NOT NULL,
                    sent_at TEXT NOT NULL,
                    PRIMARY KEY (group_id, release_date, lead_days)
                )
                """
            )
            self._deduplicate_existing(connection)

    @staticmethod
    def _row_game(row: sqlite3.Row) -> CalendarGame:
        return CalendarGame(
            app_id=str(row["app_id"]),
            name=str(row["name"]),
            release_date=date.fromisoformat(str(row["release_date"])),
            image_url=str(row["image_url"]) if row["image_url"] else None,
            platform=str(row["platform"] or "Steam"),
        )

    def _deduplicate_existing(self, connection: sqlite3.Connection) -> None:
        rows = connection.execute(
            "SELECT app_id, name, release_date, image_url, platform "
            "FROM game_releases ORDER BY release_date, updated_at"
        ).fetchall()
        kept: list[CalendarGame] = []
        for row in rows:
            game = self._row_game(row)
            duplicate_index = next(
                (
                    index
                    for index, existing in enumerate(kept)
                    if existing.platform.casefold() != game.platform.casefold()
                    and abs((existing.release_date - game.release_date).days)
                    <= CALENDAR_DUPLICATE_DAY_WINDOW
                    and game_names_highly_similar(existing.name, game.name)
                ),
                None,
            )
            if duplicate_index is None:
                kept.append(game)
                continue
            existing = kept[duplicate_index]
            if _platform_priority(game.platform) > _platform_priority(existing.platform):
                connection.execute(
                    "DELETE FROM game_releases WHERE app_id = ?", (existing.app_id,)
                )
                kept[duplicate_index] = game
                logger.info(
                    "Calendar duplicate merged into %s; removed %s",
                    game.app_id,
                    existing.app_id,
                )
            else:
                connection.execute("DELETE FROM game_releases WHERE app_id = ?", (game.app_id,))
                logger.info(
                    "Calendar duplicate kept as %s; removed %s",
                    existing.app_id,
                    game.app_id,
                )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def add(self, game: CalendarGame, added_by: str) -> bool:
        created, _ = self.add_resolved(game, added_by)
        return created

    def add_resolved(self, game: CalendarGame, added_by: str) -> tuple[bool, CalendarGame]:
        with self._connect() as connection:
            existed = connection.execute(
                "SELECT 1 FROM game_releases WHERE app_id = ?", (game.app_id,)
            ).fetchone() is not None
            start = (game.release_date - timedelta(days=CALENDAR_DUPLICATE_DAY_WINDOW)).isoformat()
            end = (game.release_date + timedelta(days=CALENDAR_DUPLICATE_DAY_WINDOW)).isoformat()
            nearby = connection.execute(
                "SELECT app_id, name, release_date, image_url, platform "
                "FROM game_releases WHERE release_date BETWEEN ? AND ? AND app_id <> ?",
                (start, end, game.app_id),
            ).fetchall()
            duplicates = [
                self._row_game(row)
                for row in nearby
                if str(row["platform"]).casefold() != game.platform.casefold()
                and game_names_highly_similar(str(row["name"]), game.name)
            ]
            if duplicates:
                existing = max(
                    duplicates,
                    key=lambda item: (
                        _platform_priority(item.platform),
                        SequenceMatcher(
                            None,
                            _comparable_game_name(item.name),
                            _comparable_game_name(game.name),
                        ).ratio(),
                    ),
                )
                if _platform_priority(existing.platform) >= _platform_priority(game.platform):
                    redundant_ids = [
                        item.app_id for item in duplicates if item.app_id != existing.app_id
                    ]
                    if redundant_ids:
                        connection.execute(
                            "DELETE FROM game_releases WHERE app_id IN ({})".format(
                                ",".join("?" for _ in redundant_ids)
                            ),
                            tuple(redundant_ids),
                        )
                    if existed:
                        connection.execute("DELETE FROM game_releases WHERE app_id = ?", (game.app_id,))
                    return False, existing
                connection.execute(
                    "DELETE FROM game_releases WHERE app_id IN ({})".format(
                        ",".join("?" for _ in duplicates)
                    ),
                    tuple(item.app_id for item in duplicates),
                )
            connection.execute(
                """
                INSERT INTO game_releases (
                    app_id, name, release_date, image_url, platform, added_by, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(app_id) DO UPDATE SET
                    name = excluded.name,
                    release_date = excluded.release_date,
                    image_url = excluded.image_url,
                    platform = excluded.platform,
                    added_by = excluded.added_by,
                    updated_at = excluded.updated_at
                """,
                (
                    game.app_id,
                    game.name,
                    game.release_date.isoformat(),
                    game.image_url,
                    game.platform,
                    added_by,
                    datetime.now().astimezone().isoformat(timespec="seconds"),
                ),
            )
        return not existed and not duplicates, game

    def list_month(self, year: int, month: int) -> list[CalendarGame]:
        prefix = f"{year:04d}-{month:02d}-%"
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT app_id, name, release_date, image_url, platform
                FROM game_releases
                WHERE release_date LIKE ?
                ORDER BY release_date, name COLLATE NOCASE
                """,
                (prefix,),
            ).fetchall()
        return [
            self._row_game(row)
            for row in rows
        ]

    def list_date(self, release_date: date) -> list[CalendarGame]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT app_id, name, release_date, image_url, platform
                FROM game_releases
                WHERE release_date = ?
                ORDER BY name COLLATE NOCASE
                """,
                (release_date.isoformat(),),
            ).fetchall()
        return [self._row_game(row) for row in rows]

    def reminder_was_sent(
        self, group_id: str, release_date: date, lead_days: int
    ) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM game_release_notifications
                WHERE group_id = ? AND release_date = ? AND lead_days = ?
                """,
                (str(group_id), release_date.isoformat(), int(lead_days)),
            ).fetchone()
        return row is not None

    def mark_reminder_sent(
        self, group_id: str, release_date: date, lead_days: int
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO game_release_notifications
                    (group_id, release_date, lead_days, sent_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    str(group_id),
                    release_date.isoformat(),
                    int(lead_days),
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                ),
            )

    def list_all(self) -> list[CalendarGame]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT app_id, name, release_date, image_url, platform
                FROM game_releases
                ORDER BY release_date, name COLLATE NOCASE
                """
            ).fetchall()
        return [
            self._row_game(row)
            for row in rows
        ]

    def get(self, app_id: str) -> CalendarGame | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT app_id, name, release_date, image_url, platform
                FROM game_releases WHERE app_id = ?
                """,
                (app_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_game(row)

    def remove(self, app_id: str) -> CalendarGame | None:
        game = self.get(app_id)
        if game is None:
            return None
        with self._connect() as connection:
            connection.execute("DELETE FROM game_releases WHERE app_id = ?", (app_id,))
        return game


def parse_release_date(value: str) -> date | None:
    text = value.strip()
    match = re.search(
        r"(?P<year>20\d{2})\s*(?:年|[-/.])\s*(?P<month>\d{1,2})\s*"
        r"(?:月|[-/.])\s*(?P<day>\d{1,2})\s*日?",
        text,
    )
    if match:
        try:
            return date(
                int(match.group("year")),
                int(match.group("month")),
                int(match.group("day")),
            )
        except ValueError:
            return None
    for pattern in ("%b %d, %Y", "%B %d, %Y", "%d %b, %Y", "%d %B, %Y"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    return None


def parse_store_release_timestamp(value: str) -> date | None:
    match = re.search(
        r"app_release_date(?:&quot;|\")\s*:\s*(?:&quot;|\")(?P<timestamp>\d{10})(?:&quot;|\")",
        value,
    )
    if match is None:
        return None
    try:
        released_at = datetime.fromtimestamp(
            int(match.group("timestamp")), tz=timezone.utc
        )
    except (OverflowError, OSError, ValueError):
        return None
    return released_at.astimezone(CHINA_TIMEZONE).date()


def _normal_name(value: str) -> str:
    return re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", value.casefold())


def _match_score(query: str, name: str, position: int) -> float:
    left = _normal_name(query)
    right = _normal_name(name)
    if not left or not right:
        return 0.0
    if left == right:
        return 200.0 - position
    containment = 45.0 if left in right or right in left else 0.0
    return SequenceMatcher(None, left, right).ratio() * 100 + containment - position


class SteamGameMatcher:
    def __init__(self, client: SteamClient | None = None) -> None:
        self.client = client or SteamClient()

    async def _china_release_date(self, game: CalendarGame) -> date:
        fetch_text = getattr(self.client, "_public_text", None)
        if fetch_text is None:
            return game.release_date
        try:
            page = await fetch_text(
                STORE_APP_URL.format(app_id=game.app_id), l="schinese", cc="cn"
            )
            precise = parse_store_release_timestamp(page)
            if precise is not None:
                return precise
        except Exception:
            logger.info(
                "Precise Steam release timestamp unavailable for app %s", game.app_id
            )
        return game.release_date

    async def lookup(self, query: str) -> CalendarGame:
        try:
            payload = await self.client._public_get(
                STORE_SEARCH_URL,
                term=query,
                l="schinese",
                cc="cn",
            )
        except Exception as exc:
            raise GameLookupError("Steam 商店暂时无法查询，请稍后再试。") from exc
        raw_items = payload.get("items")
        if not isinstance(raw_items, list):
            raise GameLookupError("没有找到这个游戏，请尝试输入更完整的官方名称。")

        candidates: list[tuple[float, CalendarGame]] = []
        for position, item in enumerate(raw_items[:8]):
            if not isinstance(item, Mapping) or item.get("type") != "app":
                continue
            app_id = str(item.get("id", "")).strip()
            if not app_id.isdigit():
                continue
            try:
                details = await self.client.game_details(app_id)
            except Exception:
                logger.info("Game calendar details unavailable for Steam app %s", app_id)
                continue
            if details.get("type") != "game":
                continue
            release = details.get("release_date")
            release_text = str(release.get("date", "")) if isinstance(release, Mapping) else ""
            release_date = parse_release_date(release_text)
            if release_date is None:
                continue
            name = str(details.get("name") or item.get("name") or query).strip()
            image_url = str(
                details.get("capsule_image")
                or details.get("header_image")
                or item.get("tiny_image")
                or ""
            ).strip() or None
            candidates.append(
                (
                    _match_score(query, name, position),
                    CalendarGame(app_id, name, release_date, image_url, "Steam"),
                )
            )

        if not candidates:
            raise GameLookupError(
                "没有匹配到已公布具体发售日的游戏，请换更准确的名称后重试。"
            )
        matched = max(candidates, key=lambda candidate: candidate[0])[1]
        release_date = await self._china_release_date(matched)
        if release_date == matched.release_date:
            return matched
        return CalendarGame(
            matched.app_id,
            matched.name,
            release_date,
            matched.image_url,
            matched.platform,
        )


WEEKDAY_NAMES = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
def parse_month_selector(value: str, today: date | None = None) -> tuple[int, int]:
    current = today or date.today()
    text = value.strip()
    full = re.fullmatch(r"(20\d{2})[-/.年](\d{1,2})月?", text)
    if full:
        year, month = int(full.group(1)), int(full.group(2))
    elif re.fullmatch(r"\d{1,2}", text):
        month = int(text)
        year = current.year + (1 if month < current.month else 0)
    else:
        raise ValueError("月份格式不正确")
    if not 1 <= month <= 12:
        raise ValueError("月份必须在 1 到 12 之间")
    return year, month


def calendar_year_months(year: int, today: date | None = None) -> list[int]:
    current = today or date.today()
    return list(range(current.month if year == current.year else 1, 13))


def _calendar_cover(
    content: bytes | None,
    size: tuple[int, int],
    *,
    fill: bool = False,
) -> Image.Image:
    background = Image.new("RGB", size, "#24394a")
    if not content:
        return background
    try:
        with Image.open(BytesIO(content)) as opened:
            source = ImageOps.exif_transpose(opened).convert("RGB")
            if fill:
                return ImageOps.fit(
                    source,
                    size,
                    method=Image.Resampling.LANCZOS,
                    centering=(0.5, 0.5),
                )
            source.thumbnail(size, Image.Resampling.LANCZOS)
            left = (size[0] - source.width) // 2
            top = (size[1] - source.height) // 2
            background.paste(source, (left, top))
    except Exception:
        pass
    return background


def _render_calendar(
    year: int,
    month: int,
    games: list[CalendarGame],
    assets: dict[str, bytes | None],
    *,
    title: str | None = None,
    subtitle: str | None = None,
) -> bytes:
    width = 760
    outer = 28
    header_height = 134
    row_height = 123
    gap = 12
    height = outer * 2 + header_height + len(games) * row_height + max(len(games) - 1, 0) * gap
    canvas = Image.new("RGB", (width, max(height, 260)), "#08131d")
    glow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow)
    glow_draw.ellipse((-180, -260, 680, 440), fill=(29, 128, 205, 70))
    glow_draw.ellipse((480, 450, 980, 1100), fill=(112, 58, 180, 35))
    canvas = Image.alpha_composite(
        canvas.convert("RGBA"), glow.filter(ImageFilter.GaussianBlur(100))
    ).convert("RGB")
    draw = ImageDraw.Draw(canvas)
    draw.text(
        (38, 38),
        title or f"{year}年{month}月游戏发售日历",
        font=_font(34, bold=True),
        fill="#f4f9fd",
    )
    draw.text(
        (40, 91),
        subtitle or f"{len(games)} 款游戏 · /game add以添加游戏",
        font=_font(16),
        fill="#8fa8bc",
    )

    top = outer + header_height
    for game in games:
        bottom = top + row_height
        draw.rounded_rectangle(
            (outer, top, width - outer, bottom),
            radius=24,
            fill="#152433",
            outline="#2b4356",
            width=2,
        )
        cover_content = assets.get(game.image_url or "")
        cover = _calendar_cover(
            cover_content,
            (231, 87),
            # Calendar covers always occupy the whole horizontal slot.  Store
            # images have several aspect ratios (PS background art, Nintendo
            # thumbnails, Steam capsules); letterboxing them made some covers
            # appear much smaller than manually uploaded images.
            fill=True,
        )
        _paste_rounded(canvas, cover, (46, top + 18), 14)
        if not cover_content:
            _render_fallback_mark(draw, (46, top + 18, 277, top + 105), game.name, "#d8e7f2")

        name, name_font = _fitted_text(draw, game.name, 286, 24, 17)
        name_bounds = draw.textbbox((0, 0), name, font=name_font)
        name_height = name_bounds[3] - name_bounds[1]
        draw.text(
            (300, top + (row_height - name_height) / 2 - name_bounds[1]),
            name,
            font=name_font,
            fill="#f4f8fb",
        )

        badge = (594, top + 20, 690, top + 98)
        draw.rounded_rectangle(badge, radius=20, fill="#0e6ca8")
        day_text = f"{game.release_date:%m.%d}"
        bounds = draw.textbbox((0, 0), day_text, font=_font(23, bold=True))
        draw.text(
            (642 - (bounds[2] - bounds[0]) / 2, top + 34),
            day_text,
            font=_font(23, bold=True),
            fill="#ffffff",
        )
        week = WEEKDAY_NAMES[game.release_date.weekday()]
        bounds = draw.textbbox((0, 0), week, font=_font(13))
        draw.text(
            (642 - (bounds[2] - bounds[0]) / 2, top + 69),
            week,
            font=_font(13),
            fill="#bfe6ff",
        )
        top = bottom + gap

    output = BytesIO()
    canvas.save(output, format="PNG", optimize=True)
    return output.getvalue()


async def _load_calendar_assets(games: list[CalendarGame]) -> dict[str, bytes | None]:
    remote_entries = [
        ListCardEntry(name=game.name, current_game=None, game_image_url=game.image_url)
        for game in games
        if game.image_url and not game.image_url.startswith("local:")
    ]
    assets = await _load_assets(remote_entries) if remote_entries else {}
    for game in games:
        if not game.image_url or not game.image_url.startswith("local:"):
            continue
        filename = game.image_url.removeprefix("local:")
        path = LOCAL_IMAGE_DIR / filename
        try:
            if path.resolve().parent == LOCAL_IMAGE_DIR.resolve():
                assets[game.image_url] = await asyncio.to_thread(path.read_bytes)
        except OSError:
            assets[game.image_url] = None
    return assets


async def build_calendar_message(year: int, month: int, games: list[CalendarGame]) -> Message:
    assets = await _load_calendar_assets(games)
    content = await asyncio.to_thread(_render_calendar, year, month, games, assets)
    return Message(MessageSegment.image(content, cache=False, proxy=False, timeout=30))


def _render_year_calendar(
    year: int,
    months: list[int],
    games: list[CalendarGame],
    assets: dict[str, bytes | None],
) -> bytes:
    panels = []
    for month in months:
        monthly = sorted(
            (game for game in games if game.release_date.year == year and game.release_date.month == month),
            key=lambda game: (game.release_date, game.name),
        )
        content = _render_calendar(
            year, month, monthly, assets,
            subtitle=None if monthly else "本月还没有登记游戏",
        )
        with Image.open(BytesIO(content)) as panel:
            panels.append(panel.convert("RGB"))
    canvas = Image.new("RGB", (sum(panel.width for panel in panels), max(panel.height for panel in panels)), "#08131d")
    left = 0
    for panel in panels:
        canvas.paste(panel, (left, 0))
        left += panel.width
        panel.close()
    output = BytesIO()
    canvas.save(output, format="PNG", optimize=True)
    return output.getvalue()


async def build_year_calendar_message(
    year: int, months: list[int], games: list[CalendarGame],
) -> Message:
    assets = await _load_calendar_assets(games)
    content = await asyncio.to_thread(_render_year_calendar, year, months, games, assets)
    return Message(MessageSegment.image(content, cache=False, proxy=False, timeout=30))


async def build_release_reminder_message(
    release_date: date,
    lead_days: int,
    games: list[CalendarGame],
) -> Message:
    remote_entries = [
        ListCardEntry(name=game.name, current_game=None, game_image_url=game.image_url)
        for game in games
        if game.image_url and not game.image_url.startswith("local:")
    ]
    assets = await _load_assets(remote_entries) if remote_entries else {}
    for game in games:
        if not game.image_url or not game.image_url.startswith("local:"):
            continue
        path = LOCAL_IMAGE_DIR / game.image_url.removeprefix("local:")
        try:
            if path.resolve().parent == LOCAL_IMAGE_DIR.resolve():
                assets[game.image_url] = await asyncio.to_thread(path.read_bytes)
        except OSError:
            assets[game.image_url] = None
    heading = "今日发售" if lead_days == 0 else "明日发售" if lead_days == 1 else f"{lead_days}天后发售"
    subtitle = f"{release_date:%Y年%m月%d日} · {len(games)} 款游戏"
    content = await asyncio.to_thread(
        _render_calendar,
        release_date.year,
        release_date.month,
        games,
        assets,
        title=heading,
        subtitle=subtitle,
    )
    return Message(MessageSegment.image(content, cache=False, proxy=False, timeout=30))

from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from functools import partial
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, TypeVar

import nonebot
from nonebot.adapters.onebot.v11 import Bot

from .game_time_tracker import tracker as game_time_tracker
from .health import mark_failure, mark_success
from .resilience import FailureBackoff, supervise, wait_for_stop
from .switch_presence import SwitchActivity, build_activity_message


logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "ps5_registry.db"
DEFAULT_NPSSO_PATH = PROJECT_ROOT / "secrets" / "psn-npsso.txt"
ONLINE_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{2,15}$")
PSN_STATIC_IMAGE_HTTP_PREFIX = "http://static-resource.np.community.playstation.net/"
T = TypeVar("T")

try:
    from psnawp_api import PSNAWP
    from psnawp_api.models.trophies import PlatformType
except ImportError:  # pragma: no cover - exercised only before dependencies are installed
    PSNAWP = None  # type: ignore[assignment]
    PlatformType = None  # type: ignore[assignment]


class Ps5Error(RuntimeError):
    """An error whose message is safe to show in a QQ group."""


class Ps5VisibilityError(Ps5Error):
    """The target has not exposed presence information to this account."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_online_id(value: str) -> str:
    online_id = value.strip()
    if not ONLINE_ID_RE.fullmatch(online_id):
        raise ValueError("PSN 在线 ID 应为 3～16 位字母、数字、下划线或连字符，并以字母开头。")
    return online_id


def psn_user_message(error: BaseException) -> str:
    detail = f"{type(error).__name__}: {error}".casefold()
    if "notfound" in detail or "not found" in detail or "404" in detail:
        return "没有找到这个 PSN 在线 ID，请检查后重新输入。"
    if "forbidden" in detail or "not allowed" in detail or "403" in detail:
        return "请把“在线状态和当前游戏”设为所有人可见"
    if "authentication" in detail or "unauthorized" in detail or "npsso" in detail or "401" in detail:
        return "PSN 观察账号登录已失效，请联系机器人管理员。"
    if "too many" in detail or "rate" in detail or "429" in detail:
        return "PSN 查询过于频繁，请稍后再试。"
    if "timeout" in detail or "timed out" in detail:
        return "PSN 服务响应超时，请稍后再试。"
    return "PSN 服务暂时无法完成操作，请稍后再试。"


def _credential_path() -> Path:
    configured = os.getenv("PSN_NPSSO_FILE", "").strip()
    if not configured:
        return DEFAULT_NPSSO_PATH
    path = Path(configured).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def _secure_psn_image_url(value: object) -> str | None:
    url = str(value or "").strip()
    if not url:
        return None
    if url.casefold().startswith(PSN_STATIC_IMAGE_HTTP_PREFIX):
        return "https://" + url[len("http://") :]
    return url


def _avatar_url(profile: Mapping[str, Any]) -> str | None:
    avatars = profile.get("avatars")
    if not isinstance(avatars, list):
        return None
    candidates = [item for item in avatars if isinstance(item, Mapping) and item.get("url")]
    for preferred in ("xl", "l", "m", "s"):
        item = next((entry for entry in candidates if str(entry.get("size", "")).lower() == preferred), None)
        if item:
            return _secure_psn_image_url(item["url"])
    return _secure_psn_image_url(candidates[0]["url"]) if candidates else None


def _catalog_cover_url(details: Any) -> str | None:
    """Pick a portrait-friendly cover from a PlayStation catalog response."""
    images: list[tuple[str, str]] = []

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            url = value.get("url")
            image_type = value.get("type")
            if isinstance(url, str) and url.startswith(("https://", "http://")) and isinstance(image_type, str):
                images.append((image_type.upper(), url))
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(details)
    for preferred in (
        "PORTRAIT_BANNER",
        "MASTER",
        "GAMEHUB_COVER_ART",
        "EDITION_KEY_ART",
        "FOUR_BY_THREE_BANNER",
        "BACKGROUND_LAYER_ART",
    ):
        match = next((url for image_type, url in images if image_type == preferred), None)
        if match:
            return match
    return None


def _presence_game(presence: Mapping[str, Any]) -> Mapping[str, Any] | None:
    games = presence.get("gameTitleInfoList")
    if not isinstance(games, list):
        return None
    return next((entry for entry in games if isinstance(entry, Mapping)), None)


@dataclass(frozen=True)
class Ps5Subscription:
    online_id: str
    account_id: str
    avatar_url: str | None
    qq_user_id: str
    group_id: str
    status: str
    current_game: str | None
    initialised: bool
    force_notify: bool = False
    nickname: str | None = None
    current_game_image_url: str | None = None


class Ps5Registry:
    def __init__(self, path: Path = DEFAULT_DB_PATH) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
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
                CREATE TABLE IF NOT EXISTS ps5_subscriptions (
                    online_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    avatar_url TEXT,
                    qq_user_id TEXT NOT NULL,
                    group_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    current_game TEXT,
                    initialised INTEGER NOT NULL DEFAULT 0,
                    force_notify INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (account_id, group_id)
                )
                """
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(ps5_subscriptions)")}
            if "nickname" not in columns:
                db.execute("ALTER TABLE ps5_subscriptions ADD COLUMN nickname TEXT")
            if "current_game_image_url" not in columns:
                db.execute("ALTER TABLE ps5_subscriptions ADD COLUMN current_game_image_url TEXT")
            # Sony still returns this default avatar on an old plain-HTTP URL.
            # Some Windows/network configurations block it, while HTTPS works.
            db.execute(
                "UPDATE ps5_subscriptions "
                "SET avatar_url = 'https://' || substr(avatar_url, 8) "
                "WHERE lower(avatar_url) LIKE ?",
                (PSN_STATIC_IMAGE_HTTP_PREFIX + "%",),
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> Ps5Subscription:
        return Ps5Subscription(
            online_id=row["online_id"],
            account_id=row["account_id"],
            avatar_url=_secure_psn_image_url(row["avatar_url"]),
            qq_user_id=row["qq_user_id"],
            group_id=row["group_id"],
            status=row["status"],
            current_game=row["current_game"],
            initialised=bool(row["initialised"]),
            force_notify=bool(row["force_notify"]),
            nickname=row["nickname"],
            current_game_image_url=row["current_game_image_url"],
        )

    def list_group(self, group_id: str) -> list[Ps5Subscription]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM ps5_subscriptions WHERE group_id = ? ORDER BY created_at",
                (group_id,),
            ).fetchall()
        return [self._row(row) for row in rows]

    def list_all(self) -> list[Ps5Subscription]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM ps5_subscriptions ORDER BY created_at").fetchall()
        return [self._row(row) for row in rows]

    def add(self, item: Ps5Subscription) -> None:
        now = utc_now()
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO ps5_subscriptions
                (online_id, account_id, avatar_url, qq_user_id, group_id, status,
                 current_game, initialised, force_notify, nickname, current_game_image_url,
                 created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.online_id,
                    item.account_id,
                    _secure_psn_image_url(item.avatar_url),
                    item.qq_user_id,
                    item.group_id,
                    item.status,
                    item.current_game,
                    int(item.initialised),
                    int(item.force_notify),
                    item.nickname,
                    item.current_game_image_url,
                    now,
                    now,
                ),
            )

    def remove(self, online_id: str, group_id: str, requester_id: str) -> bool:
        with self._connect() as db:
            result = db.execute(
                "DELETE FROM ps5_subscriptions "
                "WHERE lower(online_id) = lower(?) AND group_id = ?",
                (online_id, group_id),
            )
        return result.rowcount > 0

    def set_nickname(
        self, online_id: str, group_id: str, requester_id: str, nickname: str
    ) -> bool:
        with self._connect() as db:
            result = db.execute(
                "UPDATE ps5_subscriptions SET nickname = ?, updated_at = ? "
                "WHERE lower(online_id) = lower(?) AND group_id = ?",
                (nickname, utc_now(), online_id, group_id),
            )
        return result.rowcount > 0

    def update_presence(
        self,
        item: Ps5Subscription,
        activity: SwitchActivity,
        *,
        status: str,
        initialised: bool,
    ) -> None:
        with self._connect() as db:
            db.execute(
                """
                UPDATE ps5_subscriptions
                SET status = ?, current_game = ?, current_game_image_url = ?,
                    initialised = ?,
                    force_notify = 0, updated_at = ?
                WHERE account_id = ? AND group_id = ?
                """,
                (
                    status,
                    activity.game_name if activity.game_key else None,
                    activity.game_image_url if activity.game_key else None,
                    int(initialised),
                    utc_now(),
                    item.account_id,
                    item.group_id,
                ),
            )


    def update_avatar(self, account_id: str, avatar_url: str) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE ps5_subscriptions SET avatar_url = ?, updated_at = ? "
                "WHERE account_id = ?",
                (_secure_psn_image_url(avatar_url), utc_now(), account_id),
            )


class Ps5Client:
    def __init__(self) -> None:
        self._api: Any | None = None
        self._npsso: str | None = None
        self._lock = asyncio.Lock()
        self._cover_cache: dict[str, str | None] = {}
        # Keep blocking PSNAWP/requests traffic away from asyncio's shared
        # worker pool so local list commands remain instant during slow calls.
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="psnawp")

    @property
    def installed(self) -> bool:
        return PSNAWP is not None

    @property
    def configured(self) -> bool:
        return self.installed and _credential_path().is_file()

    def _get_api(self) -> Any:
        if PSNAWP is None:
            raise Ps5Error("PS5 功能尚未安装，请联系机器人管理员。")
        path = _credential_path()
        try:
            npsso = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise Ps5Error("PSN 观察账号尚未登录，请联系机器人管理员。") from exc
        if not npsso:
            raise Ps5Error("PSN 观察账号尚未登录，请联系机器人管理员。")
        if self._api is None or self._npsso != npsso:
            self._api = PSNAWP(npsso)
            session = self._api.authenticator.request_builder.session
            session.request = partial(session.request, timeout=(10, 20))
            self._npsso = npsso
        return self._api

    async def _call(self, operation: str, callback: Callable[[], T]) -> T:
        async with self._lock:
            try:
                loop = asyncio.get_running_loop()
                return await loop.run_in_executor(self._executor, callback)
            except Ps5Error:
                raise
            except Exception as exc:
                logger.exception("PSN operation failed: %s", operation)
                message = psn_user_message(exc)
                if message == "请把“在线状态和当前游戏”设为所有人可见":
                    raise Ps5VisibilityError(message) from exc
                raise Ps5Error(message) from exc

    async def status(self) -> str:
        def run() -> str:
            client = self._get_api().me()
            return str(client.online_id)

        return await self._call("status", run)

    async def lookup(self, online_id: str) -> Mapping[str, Any]:
        def run() -> Mapping[str, Any]:
            user = self._get_api().user(online_id=online_id)
            try:
                profile = user.profile()
            except Exception:
                logger.warning("Unable to fetch PSN profile for %s", online_id, exc_info=True)
                profile = {}
            return {
                "online_id": str(user.online_id),
                "account_id": str(user.account_id),
                "avatar_url": _avatar_url(profile),
                # Reading presence here verifies that the target can be observed
                # without adding it as a friend before we persist the record.
                "presence": user.get_presence(),
            }

        return await self._call("lookup", run)

    async def presences(self, account_ids: list[str]) -> Mapping[str, Any]:
        def run() -> Mapping[str, Any]:
            return self._get_api().me().get_presences(account_ids)

        return await self._call("presence", run)

    async def avatar(self, account_id: str) -> str | None:
        return await self._call(
            "avatar", lambda: _avatar_url(self._get_api().user(account_id=account_id).profile())
        )

    async def game_cover(self, title_id: str, platform: str) -> str | None:
        cache_key = f"{platform.upper()}:{title_id}"
        if cache_key in self._cover_cache:
            return self._cover_cache[cache_key]

        def run() -> str | None:
            if PlatformType is None:
                return None
            platform_type = PlatformType.PS5 if platform.upper() == "PS5" else PlatformType.PS4
            # Catalog details do not use the trophy communication ID. Supplying
            # a placeholder avoids an unnecessary trophy lookup during setup.
            title = self._get_api().game_title(
                title_id,
                platform_type,
                np_communication_id="unused",
            )
            return _catalog_cover_url(title.get_details())

        try:
            cover_url = await self._call("game cover", run)
        except Ps5Error:
            logger.warning("Unable to resolve PS5 cover for %s", title_id, exc_info=True)
            return None
        self._cover_cache[cache_key] = cover_url
        return cover_url


def parse_ps5_activity(item: Ps5Subscription, presence: Mapping[str, Any]) -> SwitchActivity:
    platform = presence.get("primaryPlatformInfo")
    online_status = str(platform.get("onlineStatus", "offline")) if isinstance(platform, Mapping) else "offline"
    game = _presence_game(presence)
    return SwitchActivity(
        account_name=item.online_id,
        state="PLAYING" if game else online_status.upper(),
        game_name=str(game.get("titleName")) if game and game.get("titleName") else None,
        avatar_url=item.avatar_url,
        game_image_url=str(game.get("npTitleIconUrl")) if game and game.get("npTitleIconUrl") else None,
        platform="PS",
    )


def _should_notify(item: Ps5Subscription, current_game: str | None) -> bool:
    if current_game is None:
        return False
    if item.force_notify or not item.initialised:
        return True
    previous = item.current_game.casefold() if item.current_game else None
    return current_game != previous


class Ps5PresenceMonitor:
    def __init__(self, registry: Ps5Registry, client: Ps5Client) -> None:
        self.registry = registry
        self.client = client
        self.interval = max(float(os.getenv("PS5_POLL_INTERVAL", "90")), 60)
        self._stop = asyncio.Event()
        self._avatar_refresh_date = None

    async def stop(self) -> None:
        self._stop.set()

    async def refresh_avatars(self) -> dict[str, int]:
        counts = {"checked": 0, "updated": 0, "failed": 0}
        items = await asyncio.to_thread(self.registry.list_all)
        for account_id in dict.fromkeys(item.account_id for item in items):
            counts["checked"] += 1
            try:
                avatar = await self.client.avatar(account_id)
                if not avatar:
                    raise Ps5Error("头像资料为空，保留现有头像")
                await asyncio.to_thread(self.registry.update_avatar, account_id, avatar)
                counts["updated"] += 1
            except Exception:
                counts["failed"] += 1
                logger.warning("Unable to refresh PSN avatar for %s", account_id, exc_info=True)
        logger.info("PSN avatar refresh complete: %s", counts)
        return counts

    async def refresh_avatars_if_due(self) -> None:
        today = datetime.now().date()
        if self._avatar_refresh_date != today:
            try:
                await asyncio.wait_for(self.refresh_avatars(), timeout=30)
            except TimeoutError:
                logger.warning("PSN avatar refresh timed out; keeping existing avatars")
            self._avatar_refresh_date = today

    async def poll_once(self) -> None:
        subscriptions = await asyncio.to_thread(self.registry.list_all)
        if not subscriptions or not self.client.configured:
            return
        account_ids = list(dict.fromkeys(item.account_id for item in subscriptions))
        presences: dict[str, Mapping[str, Any]] = {}
        for start in range(0, len(account_ids), 50):
            payload = await self.client.presences(account_ids[start : start + 50])
            values = payload.get("basicPresences", [])
            if isinstance(values, list):
                presences.update(
                    {
                        str(value["accountId"]): value
                        for value in values
                        if isinstance(value, Mapping) and value.get("accountId")
                    }
                )

        polled_at = datetime.now(timezone.utc)
        bots = [bot for bot in nonebot.get_bots().values() if isinstance(bot, Bot)]
        for item in subscriptions:
            presence = presences.get(item.account_id)
            if presence is None:
                continue
            activity = parse_ps5_activity(item, presence)
            if activity.game_key and not activity.game_image_url:
                game = _presence_game(presence)
                title_id = str(game.get("npTitleId", "")) if game else ""
                platform = str(game.get("launchPlatform", "PS5")) if game else "PS5"
                if title_id:
                    try:
                        cover_url = await asyncio.wait_for(
                            self.client.game_cover(title_id, platform), timeout=12
                        )
                        if cover_url:
                            activity = replace(activity, game_image_url=cover_url)
                    except TimeoutError:
                        logger.warning("PS5 cover lookup timed out for title %s", title_id)
                    except Exception as exc:
                        logger.warning("PS5 cover lookup failed for title %s: %s", title_id, exc)
            await asyncio.to_thread(
                game_time_tracker.observe,
                platform="ps",
                account_id=item.account_id,
                group_id=item.group_id,
                display_name=item.nickname or item.online_id,
                avatar_url=activity.avatar_url or item.avatar_url,
                game_key=activity.game_key,
                game_name=activity.game_name,
                game_image_url=activity.game_image_url or (
                    item.current_game_image_url
                    if item.current_game and activity.game_key == item.current_game.casefold()
                    else None
                ),
                observed_at=polled_at,
            )
            should_notify = _should_notify(item, activity.game_key)
            sent = False
            if should_notify and bots:
                try:
                    message = await asyncio.wait_for(
                        build_activity_message(item.nickname or item.online_id, activity), timeout=15
                    )
                    await asyncio.wait_for(
                        bots[0].send_group_msg(
                            group_id=int(item.group_id),
                            message=message,
                        ),
                        timeout=15,
                    )
                    sent = True
                    logger.info("Sent PS5 activity for %s to group %s", item.online_id, item.group_id)
                except TimeoutError:
                    logger.warning("PS5 activity notification timed out for %s", item.online_id)
                except Exception:
                    logger.exception("Failed to send PS5 activity for %s to group %s", item.online_id, item.group_id)
            if not should_notify or sent:
                await asyncio.to_thread(
                    self.registry.update_presence,
                    item,
                    activity,
                    status="active",
                    initialised=True,
                )
        await asyncio.to_thread(game_time_tracker.mark_poll, "ps", polled_at)

    async def run(self) -> None:
        backoff = FailureBackoff(self.interval, 900.0)
        while not self._stop.is_set():
            if not self.client.configured:
                mark_failure("ps5", "观察账号尚未登录")
                await wait_for_stop(self._stop, 300.0)
                continue
            try:
                await asyncio.wait_for(self.poll_once(), timeout=60)
                mark_success("ps5", "监控正常")
                backoff.success()
                delay = self.interval
                await self.refresh_avatars_if_due()
            except TimeoutError:
                mark_failure("ps5", "PSN 请求超时，正在自动重试")
                delay = backoff.failure()
                if backoff.should_log():
                    logger.warning("PS5 polling timed out; retry in %.0fs", delay)
            except Ps5Error as exc:
                mark_failure("ps5", str(exc))
                delay = backoff.failure()
                if backoff.should_log():
                    logger.warning("PS5 monitoring unavailable: %s; retry in %.0fs", exc, delay)
            except Exception as exc:
                mark_failure("ps5", "内部异常，正在自动恢复")
                delay = backoff.failure()
                if backoff.should_log():
                    logger.error(
                        "Unexpected PS5 monitoring failure: %s; retry in %.0fs",
                        type(exc).__name__,
                        delay,
                        exc_info=True,
                    )
            now = datetime.now()
            midnight = datetime.combine(now.date() + timedelta(days=1), datetime.min.time())
            await wait_for_stop(self._stop, min(delay, (midnight - now).total_seconds()))


registry = Ps5Registry(Path(os.getenv("PS5_DB_PATH", DEFAULT_DB_PATH)))
ps5_client = Ps5Client()
_monitor: Ps5PresenceMonitor | None = None
_task: asyncio.Task[None] | None = None


async def start_ps5_monitor() -> None:
    global _monitor, _task
    _monitor = Ps5PresenceMonitor(registry, ps5_client)
    _task = asyncio.create_task(
        supervise("ps5-presence-monitor", _monitor.run, _monitor._stop.is_set),
        name="ps5-presence-monitor-supervisor",
    )
    logger.info("PS5 presence monitor ready (configured=%s)", ps5_client.configured)


async def stop_ps5_monitor() -> None:
    global _monitor, _task
    if _monitor:
        await _monitor.stop()
    if _task:
        await _task
    _monitor = None
    _task = None

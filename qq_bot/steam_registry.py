from __future__ import annotations

import asyncio
import logging
import os
import re
import ssl
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

import httpx
import nonebot
import truststore
from nonebot.adapters.onebot.v11 import Bot

from .game_time_tracker import tracker as game_time_tracker
from .health import mark_failure, mark_success
from .resilience import FailureBackoff, supervise, wait_for_stop
from .switch_presence import SwitchActivity, build_activity_message


logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "steam_registry.db"
DEFAULT_KEY_PATH = PROJECT_ROOT / "secrets" / "steam-api-key.txt"
DEFAULT_STEAMGRIDDB_KEY_PATH = PROJECT_ROOT / "secrets" / "steamgriddb-api-key.txt"
API_ROOT = "https://api.steampowered.com"
STEAMGRIDDB_API_ROOT = "https://www.steamgriddb.com/api/v2"
STEAM_ID_RE = re.compile(r"^\d{17}$")
STEAM_ACCOUNT_ID_BASE = 76561197960265728
VANITY_RE = re.compile(r"^[A-Za-z0-9_-]{2,64}$")
CJK_RE = re.compile(r"[\u3400-\u9fff]")


class SteamError(RuntimeError):
    """An error whose text is safe to send to a QQ group."""


class SteamVisibilityError(SteamError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _key_path() -> Path:
    configured = os.getenv("STEAM_API_KEY_FILE", "").strip()
    if not configured:
        return DEFAULT_KEY_PATH
    path = Path(configured).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def _steamgriddb_key_path() -> Path:
    configured = os.getenv("STEAMGRIDDB_API_KEY_FILE", "").strip()
    if not configured:
        return DEFAULT_STEAMGRIDDB_KEY_PATH
    path = Path(configured).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def normalize_steam_identifier(value: str) -> str:
    identifier = value.strip().rstrip("/")
    if not identifier:
        raise ValueError("请填写 Steam 好友码、SteamID64、Steam 个人主页链接或自定义 ID。")
    if STEAM_ID_RE.fullmatch(identifier):
        return identifier
    if re.fullmatch(r"[0-9]+", identifier):
        if len(identifier) > 10 or not 0 < int(identifier) <= 0xFFFFFFFF:
            raise ValueError("Steam 好友码应为有效的纯数字好友码，请从 Steam“添加好友”页面复制。")
        return str(STEAM_ACCOUNT_ID_BASE + int(identifier))
    if identifier.startswith(("http://", "https://")):
        parsed = urlparse(identifier)
        if (parsed.hostname or "").casefold() not in {"steamcommunity.com", "www.steamcommunity.com"}:
            raise ValueError("只支持 steamcommunity.com 的个人主页链接。")
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) != 2 or parts[0].casefold() not in {"profiles", "id"}:
            raise ValueError("Steam 个人主页链接格式不正确。")
        candidate = parts[1]
        if parts[0].casefold() == "profiles" and not STEAM_ID_RE.fullmatch(candidate):
            raise ValueError("SteamID64 应为 17 位数字。")
        if parts[0].casefold() == "id" and not VANITY_RE.fullmatch(candidate):
            raise ValueError("Steam 自定义 ID 格式不正确。")
        return candidate
    if VANITY_RE.fullmatch(identifier):
        return identifier
    raise ValueError("请填写 Steam 好友码、SteamID64、Steam 个人主页链接或自定义 ID。")


def steam_user_message(error: BaseException) -> str:
    detail = f"{type(error).__name__}: {error}".casefold()
    if "401" in detail or "403" in detail or "invalid api key" in detail:
        return "Steam Web API Key 无效，请联系机器人管理员。"
    if "429" in detail or "rate" in detail or "too many" in detail:
        return "Steam 查询过于频繁，请稍后再试。"
    if "timeout" in detail or "timed out" in detail:
        return "Steam 服务响应超时，请稍后再试。"
    return "Steam 服务暂时无法完成操作，请稍后再试。"


@dataclass(frozen=True)
class SteamSubscription:
    steam_id: str
    display_name: str
    identifier: str
    avatar_url: str | None
    qq_user_id: str
    group_id: str
    status: str
    current_game: str | None
    initialised: bool
    force_notify: bool = False
    nickname: str | None = None
    current_game_image_url: str | None = None


class SteamRegistry:
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
                CREATE TABLE IF NOT EXISTS steam_subscriptions (
                    steam_id TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    identifier TEXT NOT NULL,
                    avatar_url TEXT,
                    qq_user_id TEXT NOT NULL,
                    group_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    current_game TEXT,
                    initialised INTEGER NOT NULL DEFAULT 0,
                    force_notify INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (steam_id, group_id)
                )
                """
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(steam_subscriptions)")}
            if "nickname" not in columns:
                db.execute("ALTER TABLE steam_subscriptions ADD COLUMN nickname TEXT")
            if "current_game_image_url" not in columns:
                db.execute("ALTER TABLE steam_subscriptions ADD COLUMN current_game_image_url TEXT")
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS steam_localized_game_names (
                    app_id TEXT PRIMARY KEY,
                    localized_name TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS steam_registry_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            cache_version = db.execute(
                "SELECT value FROM steam_registry_meta WHERE key = 'localized_name_cache_version'"
            ).fetchone()
            if cache_version is None or str(cache_version[0]) != "2":
                # Older builds could associate the previous game's translated title
                # with the newly observed AppID. Discard those unverified mappings once;
                # future entries are sourced only from Store appdetails for that AppID.
                db.execute("DELETE FROM steam_localized_game_names")
                db.execute(
                    """
                    INSERT INTO steam_registry_meta (key, value) VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    ("localized_name_cache_version", "2"),
                )

    @staticmethod
    def _row(row: sqlite3.Row) -> SteamSubscription:
        return SteamSubscription(
            steam_id=row["steam_id"],
            display_name=row["display_name"],
            identifier=row["identifier"],
            avatar_url=row["avatar_url"],
            qq_user_id=row["qq_user_id"],
            group_id=row["group_id"],
            status=row["status"],
            current_game=row["current_game"],
            initialised=bool(row["initialised"]),
            force_notify=bool(row["force_notify"]),
            nickname=row["nickname"],
            current_game_image_url=row["current_game_image_url"],
        )

    def list_group(self, group_id: str) -> list[SteamSubscription]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM steam_subscriptions WHERE group_id = ? ORDER BY created_at",
                (group_id,),
            ).fetchall()
        return [self._row(row) for row in rows]

    def list_all(self) -> list[SteamSubscription]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM steam_subscriptions ORDER BY created_at").fetchall()
        return [self._row(row) for row in rows]

    def add(self, item: SteamSubscription) -> None:
        now = utc_now()
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO steam_subscriptions
                (steam_id, display_name, identifier, avatar_url, qq_user_id, group_id,
                 status, current_game, initialised, force_notify, nickname,
                 current_game_image_url, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.steam_id, item.display_name, item.identifier, item.avatar_url,
                    item.qq_user_id, item.group_id, item.status, item.current_game,
                    int(item.initialised), int(item.force_notify), item.nickname,
                    item.current_game_image_url, now, now,
                ),
            )

    def remove(self, identifier: str, group_id: str, requester_id: str) -> bool:
        with self._connect() as db:
            result = db.execute(
                "DELETE FROM steam_subscriptions WHERE group_id = ? "
                "AND (steam_id = ? OR lower(identifier) = lower(?) OR lower(display_name) = lower(?))",
                (group_id, identifier, identifier, identifier),
            )
        return result.rowcount > 0

    def set_nickname(
        self, identifier: str, group_id: str, requester_id: str, nickname: str
    ) -> bool:
        with self._connect() as db:
            result = db.execute(
                "UPDATE steam_subscriptions SET nickname = ?, updated_at = ? "
                "WHERE group_id = ? "
                "AND (steam_id = ? OR lower(identifier) = lower(?) OR lower(display_name) = lower(?))",
                (nickname, utc_now(), group_id, identifier, identifier, identifier),
            )
        return result.rowcount > 0

    def update_presence(self, item: SteamSubscription, activity: SwitchActivity) -> None:
        with self._connect() as db:
            db.execute(
                """
                UPDATE steam_subscriptions
                SET display_name = ?, avatar_url = ?, status = 'active', current_game = ?,
                    current_game_image_url = ?,
                    initialised = 1, force_notify = 0, updated_at = ?
                WHERE steam_id = ? AND group_id = ?
                """,
                (
                    activity.account_name, activity.avatar_url or item.avatar_url,
                    activity.game_name if activity.game_key else None,
                    activity.game_image_url if activity.game_key else None, utc_now(),
                    item.steam_id, item.group_id,
                ),
            )

    def localized_game_name(self, app_id: str) -> str | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT localized_name FROM steam_localized_game_names WHERE app_id = ?",
                (app_id,),
            ).fetchone()
        return str(row[0]).strip() if row and str(row[0]).strip() else None

    def remember_localized_game_name(self, app_id: str, name: str) -> bool:
        value = name.strip()
        if not app_id.isdigit() or not value or not CJK_RE.search(value):
            return False
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO steam_localized_game_names (app_id, localized_name, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(app_id) DO UPDATE SET
                    localized_name = excluded.localized_name,
                    updated_at = excluded.updated_at
                """,
                (app_id, value, utc_now()),
            )
        return True


class SteamClient:
    def __init__(self, registry: SteamRegistry | None = None) -> None:
        self._registry = registry
        self._cover_cache: dict[str, str | None] = {}
        self._metadata_cache: dict[str, Mapping[str, Any]] = {}
        self._metadata_tasks: dict[str, asyncio.Task[Mapping[str, Any]]] = {}
        self._localized_name_cache: dict[str, str] = {}
        self._cache_limit = max(int(os.getenv("STEAM_RUNTIME_CACHE_MAX_ITEMS", "512")), 32)

    def _remember(self, cache: dict[str, Any], key: str, value: Any) -> None:
        cache[key] = value
        while len(cache) > self._cache_limit:
            cache.pop(next(iter(cache)))

    @property
    def configured(self) -> bool:
        return _key_path().is_file()

    def _key(self) -> str:
        try:
            key = _key_path().read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise SteamError("Steam Web API Key 尚未配置，请联系机器人管理员。") from exc
        if not re.fullmatch(r"[A-Fa-f0-9]{32}", key):
            raise SteamError("Steam Web API Key 无效，请联系机器人管理员。")
        return key

    @property
    def steamgriddb_configured(self) -> bool:
        return _steamgriddb_key_path().is_file()

    def _steamgriddb_key(self) -> str | None:
        try:
            key = _steamgriddb_key_path().read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return key if len(key) >= 16 else None

    async def _get(self, path: str, **params: str) -> Mapping[str, Any]:
        quiet_statuses = set(params.pop("_quiet_statuses", ()))
        params["key"] = self._key()
        last_error: Exception | None = None
        # A number of Steam edge nodes occasionally close the connection while
        # response headers are being read. httpx exposes that as ReadError,
        # which is a TransportError and is safe to retry. Try direct first,
        # then allow an environment proxy as a fallback on this computer.
        trust_modes = (False, True, True)
        for attempt, trust_env in enumerate(trust_modes):
            try:
                timeout = httpx.Timeout(35.0, connect=15.0)
                context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                async with httpx.AsyncClient(
                    timeout=timeout,
                    follow_redirects=True,
                    verify=context,
                    trust_env=trust_env,
                ) as client:
                    response = await client.get(f"{API_ROOT}/{path.lstrip('/')}", params=params)
                    response.raise_for_status()
                    payload = response.json()
                if not isinstance(payload, Mapping):
                    raise ValueError("invalid Steam response")
                return payload
            except httpx.HTTPStatusError as exc:
                last_error = exc
                if exc.response.status_code < 500 and exc.response.status_code != 429:
                    break
            except httpx.TransportError as exc:
                last_error = exc
            except Exception as exc:
                last_error = exc
                break
            if attempt < len(trust_modes) - 1:
                logger.info(
                    "Steam API temporary failure (%s), retry %d/%d",
                    type(last_error).__name__,
                    attempt + 2,
                    len(trust_modes),
                )
                await asyncio.sleep(1 << attempt)
        assert last_error is not None
        status = getattr(getattr(last_error, "response", None), "status_code", None)
        log = logger.info if status in quiet_statuses else logger.warning
        log(
            "Steam API request failed after retries: endpoint=%s status=%s error=%s",
            path, status if status is not None else "network", type(last_error).__name__,
        )
        error = SteamError(steam_user_message(last_error))
        error.steam_status = status
        raise error from None

    async def _public_get(self, url: str, **params: str) -> Mapping[str, Any]:
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(30.0, connect=15.0),
                    follow_redirects=True,
                    verify=context,
                    trust_env=False,
                ) as client:
                    response = await client.get(url, params=params)
                    response.raise_for_status()
                    payload = response.json()
                if isinstance(payload, Mapping):
                    return payload
                raise ValueError("invalid Steam store response")
            except Exception as exc:
                last_error = exc
                if attempt == 0:
                    await asyncio.sleep(1)
        raise last_error or SteamError("Steam 商店图片暂时不可用。")

    async def _public_text(self, url: str, **params: str) -> str:
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(30.0, connect=15.0),
                    follow_redirects=True,
                    verify=context,
                    trust_env=False,
                ) as client:
                    response = await client.get(url, params=params)
                    response.raise_for_status()
                    return response.text
            except Exception as exc:
                last_error = exc
                if attempt == 0:
                    await asyncio.sleep(1)
        raise last_error or SteamError("Steam 商店页面暂时不可用。")

    async def _url_available(self, url: str) -> bool:
        try:
            context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(20.0, connect=10.0),
                follow_redirects=True,
                verify=context,
                trust_env=False,
            ) as client:
                async with client.stream("GET", url) as response:
                    response.raise_for_status()
                    return response.headers.get("content-type", "").casefold().startswith("image/")
        except httpx.HTTPError:
            return False

    async def _fetch_game_details(self, app_id: str) -> Mapping[str, Any]:
        payload = await self._public_get(
            "https://store.steampowered.com/api/appdetails",
            appids=app_id,
            l="schinese",
            cc="cn",
        )
        entry = payload.get(app_id)
        data = entry.get("data") if isinstance(entry, Mapping) and entry.get("success") else None
        return data if isinstance(data, Mapping) else {}

    async def game_details(self, app_id: str) -> Mapping[str, Any]:
        if app_id in self._metadata_cache:
            return self._metadata_cache[app_id]
        task = self._metadata_tasks.get(app_id)
        if task is None:
            task = asyncio.create_task(self._fetch_game_details(app_id))
            self._metadata_tasks[app_id] = task
        try:
            details = await asyncio.shield(task)
        finally:
            if task.done():
                self._metadata_tasks.pop(app_id, None)
        self._remember(self._metadata_cache, app_id, details)
        return details

    async def game_name(self, app_id: str) -> str | None:
        cached = self._localized_name_cache.get(app_id)
        if cached:
            return cached
        if self._registry is not None:
            cached = await asyncio.to_thread(self._registry.localized_game_name, app_id)
            if cached:
                self._remember(self._localized_name_cache, app_id, cached)
                return cached
        details = await self.game_details(app_id)
        name = str(details.get("name", "")).strip()
        if name and CJK_RE.search(name):
            self._remember(self._localized_name_cache, app_id, name)
            if self._registry is not None:
                await asyncio.to_thread(
                    self._registry.remember_localized_game_name, app_id, name
                )
        return name or None

    async def _steamgriddb_cover(self, app_id: str) -> str | None:
        key = self._steamgriddb_key()
        if not key:
            return None
        try:
            context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(4.0, connect=2.5),
                follow_redirects=True,
                verify=context,
                trust_env=False,
                headers={"Authorization": f"Bearer {key}"},
            ) as client:
                response = await client.get(
                    f"{STEAMGRIDDB_API_ROOT}/grids/steam/{app_id}",
                    params={
                        "dimensions": "600x900",
                        "types": "static",
                        "nsfw": "false",
                    },
                )
                response.raise_for_status()
                payload = response.json()
            values = payload.get("data", []) if isinstance(payload, Mapping) else []
            candidates = [
                value
                for value in values
                if isinstance(value, Mapping)
                and str(value.get("url", "")).startswith("https://")
            ]
            if not candidates:
                return None
            candidates.sort(
                key=lambda value: (
                    int(value.get("width", 0)) == 600 and int(value.get("height", 0)) == 900,
                    int(value.get("score", 0)),
                ),
                reverse=True,
            )
            return str(candidates[0]["url"])
        except Exception:
            # Cover enrichment is optional and must never interrupt presence polling.
            logger.warning("SteamGridDB cover lookup failed for app %s", app_id, exc_info=True)
            return None

    async def game_cover(self, app_id: str) -> str | None:
        if app_id in self._cover_cache:
            return self._cover_cache[app_id]
        conventional = (
            "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/"
            f"{app_id}/library_600x900_2x.jpg"
        )
        if await self._url_available(conventional):
            self._remember(self._cover_cache, app_id, conventional)
            return conventional
        steamgriddb = await self._steamgriddb_cover(app_id)
        if steamgriddb:
            self._remember(self._cover_cache, app_id, steamgriddb)
            return steamgriddb
        try:
            data = await self.game_details(app_id)
            cover = str(data.get("header_image", ""))
            result = cover if cover.startswith("https://") else None
        except Exception:
            logger.warning("Unable to resolve Steam cover for app %s", app_id, exc_info=True)
            result = None
        self._remember(self._cover_cache, app_id, result)
        return result

    async def status(self) -> None:
        await self._get("ISteamUser/ResolveVanityURL/v1/", vanityurl="gaben")

    async def resolve(self, identifier: str) -> str:
        # Explicit /id/ links remain vanity IDs, including all-numeric names.
        parsed = urlparse(identifier.strip())
        vanity_link = parsed.path.lower().startswith("/id/") and bool(parsed.scheme)
        identifier = normalize_steam_identifier(identifier)
        if not vanity_link and STEAM_ID_RE.fullmatch(identifier):
            return identifier
        payload = await self._get("ISteamUser/ResolveVanityURL/v1/", vanityurl=identifier)
        response = payload.get("response")
        if not isinstance(response, Mapping) or int(response.get("success", 0)) != 1:
            raise SteamError("没有找到这个 Steam 用户，请检查后重新输入。")
        steam_id = str(response.get("steamid", ""))
        if not STEAM_ID_RE.fullmatch(steam_id):
            raise SteamError("没有找到这个 Steam 用户，请检查后重新输入。")
        return steam_id

    async def players(self, steam_ids: list[str]) -> dict[str, Mapping[str, Any]]:
        result: dict[str, Mapping[str, Any]] = {}
        for start in range(0, len(steam_ids), 100):
            payload = await self._get(
                "ISteamUser/GetPlayerSummaries/v2/",
                steamids=",".join(steam_ids[start : start + 100]),
            )
            response = payload.get("response")
            values = response.get("players", []) if isinstance(response, Mapping) else []
            if isinstance(values, list):
                result.update(
                    {str(player["steamid"]): player for player in values
                     if isinstance(player, Mapping) and player.get("steamid")}
                )
        return result

    async def lookup(self, identifier: str) -> Mapping[str, Any]:
        steam_id = await self.resolve(identifier)
        player = (await self.players([steam_id])).get(steam_id)
        if player is None:
            raise SteamError("没有找到这个 Steam 用户，请检查后重新输入。")
        if int(player.get("communityvisibilitystate", 0)) != 3:
            raise SteamVisibilityError("请把 Steam“我的个人资料”和“游戏详情”设为公开")
        return player


def parse_steam_activity(item: SteamSubscription, player: Mapping[str, Any]) -> SwitchActivity:
    app_id = str(player.get("gameid", "")).strip()
    game_name = str(player.get("gameextrainfo", "")).strip() or None
    return SwitchActivity(
        account_name=str(player.get("personaname") or item.display_name),
        state="PLAYING" if app_id and game_name else "ONLINE" if int(player.get("personastate", 0)) else "OFFLINE",
        game_name=game_name,
        avatar_url=str(player.get("avatarfull") or item.avatar_url or "") or None,
        game_image_url=(
            f"https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/"
            f"{app_id}/library_600x900_2x.jpg"
            if app_id else None
        ),
        platform="Steam",
    )


def _should_notify(
    item: SteamSubscription,
    current_game: str | None,
    *display_aliases: str | None,
) -> bool:
    if current_game is None:
        return False
    if item.force_notify or not item.initialised:
        return True
    previous = item.current_game.casefold() if item.current_game else None
    current_names = {
        value.casefold()
        for value in (current_game, *display_aliases)
        if value
    }
    return previous not in current_names


class SteamPresenceMonitor:
    def __init__(self, registry: SteamRegistry, client: SteamClient) -> None:
        self.registry = registry
        self.client = client
        self.interval = max(float(os.getenv("STEAM_POLL_INTERVAL", "90")), 60)
        self._stop = asyncio.Event()

    async def stop(self) -> None:
        self._stop.set()

    async def poll_once(self) -> None:
        items = await asyncio.to_thread(self.registry.list_all)
        if not items or not self.client.configured:
            return
        steam_ids = list(dict.fromkeys(item.steam_id for item in items))
        players = await self.client.players(steam_ids)
        from .steam_achievements import observe_players
        observe_players(players)
        polled_at = datetime.now(timezone.utc)
        bots = [bot for bot in nonebot.get_bots().values() if isinstance(bot, Bot)]
        for item in items:
            player = players.get(item.steam_id)
            if player is None:
                continue
            activity = parse_steam_activity(item, player)
            app_id = str(player.get("gameid", "")).strip()
            raw_game_key = activity.game_key
            if activity.game_key and app_id:
                name_result, cover_result = await asyncio.gather(
                    asyncio.wait_for(self.client.game_name(app_id), timeout=8),
                    asyncio.wait_for(self.client.game_cover(app_id), timeout=12),
                    return_exceptions=True,
                )
                if isinstance(name_result, str) and name_result.strip():
                    activity = replace(activity, game_name=name_result.strip())
                elif isinstance(name_result, BaseException):
                    logger.warning("Steam Chinese title lookup failed for app %s: %s", app_id, name_result)
                if isinstance(cover_result, str) and cover_result:
                    activity = replace(activity, game_image_url=cover_result)
                elif isinstance(cover_result, BaseException):
                    logger.warning("Steam cover lookup failed for app %s: %s", app_id, cover_result)
            await asyncio.to_thread(
                game_time_tracker.observe,
                platform="steam",
                account_id=item.steam_id,
                group_id=item.group_id,
                display_name=item.nickname or activity.account_name,
                avatar_url=activity.avatar_url or item.avatar_url,
                # PlayerSummaries' original title remains the stable identity;
                # the Store-localised title is presentation only.
                game_key=raw_game_key,
                game_name=activity.game_name,
                game_image_url=activity.game_image_url or item.current_game_image_url,
                observed_at=polled_at,
            )
            should_notify = _should_notify(item, raw_game_key, activity.game_key)
            sent = False
            if should_notify and bots:
                try:
                    message = await asyncio.wait_for(
                        build_activity_message(item.nickname or activity.account_name, activity), timeout=15
                    )
                    await asyncio.wait_for(
                        bots[0].send_group_msg(
                            group_id=int(item.group_id),
                            message=message,
                        ),
                        timeout=15,
                    )
                    sent = True
                    logger.info("Sent Steam activity for %s to group %s", item.steam_id, item.group_id)
                except TimeoutError:
                    logger.warning("Steam activity notification timed out for %s", item.steam_id)
                except Exception:
                    logger.exception("Failed to send Steam activity for %s", item.steam_id)
            if not should_notify or sent:
                await asyncio.to_thread(self.registry.update_presence, item, activity)
        await asyncio.to_thread(game_time_tracker.mark_poll, "steam", polled_at)

    async def run(self) -> None:
        backoff = FailureBackoff(self.interval, 900.0)
        while not self._stop.is_set():
            if not self.client.configured:
                mark_failure("steam", "API Key 尚未配置")
                await wait_for_stop(self._stop, 300.0)
                continue
            try:
                await self.poll_once()
                mark_success("steam", "视奸正常")
                backoff.success()
                delay = self.interval
            except SteamError as exc:
                mark_failure("steam", str(exc))
                delay = backoff.failure()
                if backoff.should_log():
                    logger.warning("Steam monitoring unavailable: %s; retry in %.0fs", exc, delay)
            except Exception as exc:
                mark_failure("steam", "内部异常，正在自动恢复")
                delay = backoff.failure()
                if backoff.should_log():
                    logger.error("Unexpected Steam monitor failure: %s", type(exc).__name__, exc_info=True)
            await wait_for_stop(self._stop, delay)


registry = SteamRegistry(Path(os.getenv("STEAM_DB_PATH", DEFAULT_DB_PATH)))
steam_client = SteamClient(registry)
_monitor: SteamPresenceMonitor | None = None
_task: asyncio.Task[None] | None = None


async def start_steam_monitor() -> None:
    global _monitor, _task
    _monitor = SteamPresenceMonitor(registry, steam_client)
    _task = asyncio.create_task(
        supervise("steam-presence-monitor", _monitor.run, _monitor._stop.is_set),
        name="steam-presence-monitor-supervisor",
    )
    logger.info("Steam presence monitor ready (configured=%s)", steam_client.configured)


async def stop_steam_monitor() -> None:
    global _monitor, _task
    if _monitor:
        await _monitor.stop()
    if _task:
        await _task
    _monitor = None
    _task = None

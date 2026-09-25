from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote

import httpx
import nonebot
from nonebot.adapters.onebot.v11 import Bot

from .game_time_tracker import tracker as game_time_tracker
from .health import mark_failure, mark_success
from .interprocess_lock import AsyncInterProcessFileLock, atomic_write_text, lock_path_for
from .resilience import FailureBackoff, supervise, wait_for_stop
from .switch_presence import SwitchActivity, build_activity_message


logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "xbox_registry.db"
DEFAULT_KEY_PATH = PROJECT_ROOT / "secrets" / "openxbl-api-key.txt"
DEFAULT_TOKEN_PATH = PROJECT_ROOT / "secrets" / "xbox-tokens.json"
DEFAULT_CLIENT_ID = "388ea51c-0b25-4029-aae2-17df49d23905"
DEFAULT_REDIRECT_URI = "http://localhost:8080/auth/callback"
API_ROOT = "https://api.xbl.io/v2"
GAMERTAG_RE = re.compile(r"^[^\x00-\x1f]{1,16}(?:#[0-9]{1,4})?$")
SYSTEM_TITLES = {
    "home", "online", "xbox", "xbox app", "xbox guide", "xbox game bar",
    "xbox mobile app", "game bar", "guide", "microsoft store", "settings",
    "gaming services", "xbox accessories",
}


class XboxError(RuntimeError):
    """An error whose message is safe to show in a QQ group."""


class XboxVisibilityError(XboxError):
    """The target has hidden presence from the observer account."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_gamertag(value: str) -> str:
    if any(ord(character) < 32 for character in value):
        raise ValueError("Xbox 玩家代号不能包含控制字符。")
    gamertag = " ".join(value.strip().split())
    if not GAMERTAG_RE.fullmatch(gamertag):
        raise ValueError("Xbox 玩家代号应为 1～16 个可见字符，可包含空格或 #数字后缀。")
    return gamertag


def _credential_path() -> Path:
    configured = os.getenv("OPENXBL_API_KEY_FILE", "").strip()
    if not configured:
        return DEFAULT_KEY_PATH
    path = Path(configured).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def _token_path() -> Path:
    configured = os.getenv("XBOX_TOKENS_FILE", "").strip()
    if not configured:
        return DEFAULT_TOKEN_PATH
    path = Path(configured).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def xbox_user_message(error: BaseException) -> str:
    detail = f"{type(error).__name__}: {error}".casefold()
    if "404" in detail or "not found" in detail:
        return "没有找到这个 Xbox 玩家代号，请检查后重新输入。"
    if "401" in detail or "invalid api" in detail or "unauthorized" in detail:
        return "Xbox API Key 已失效，请联系机器人管理员。"
    if "403" in detail or "privacy" in detail or "forbidden" in detail:
        return "请把 Xbox 的在线状态和当前游戏设为所有人可见"
    if "429" in detail or "rate limit" in detail:
        return "Xbox 查询额度暂时用完，请稍后再试。"
    if "timeout" in detail or "timed out" in detail:
        return "Xbox 服务响应超时，请稍后再试。"
    return "Xbox 服务暂时无法完成操作，请稍后再试。"


def _content(payload: Any) -> Any:
    while isinstance(payload, Mapping) and set(payload).intersection({"content", "data"}):
        child = payload.get("content", payload.get("data"))
        if child is payload or child is None:
            break
        payload = child
    return payload


def _profile_values(profile: Mapping[str, Any]) -> dict[str, str]:
    values: dict[str, str] = {}
    settings = profile.get("settings")
    if isinstance(settings, list):
        for item in settings:
            if isinstance(item, Mapping) and item.get("id") and item.get("value") is not None:
                values[str(item["id"]).casefold()] = str(item["value"])
    return values


def parse_xbox_profile(payload: Any, requested: str) -> dict[str, str | None]:
    payload = _content(payload)
    candidates: list[Mapping[str, Any]] = []
    if isinstance(payload, Mapping):
        for key in ("people", "profileUsers", "profiles", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                candidates.extend(item for item in value if isinstance(item, Mapping))
        if any(key in payload for key in ("xuid", "id", "gamertag", "modernGamertag")):
            candidates.append(payload)
    elif isinstance(payload, list):
        candidates.extend(item for item in payload if isinstance(item, Mapping))
    if not candidates:
        raise XboxError("没有找到这个 Xbox 玩家代号，请检查后重新输入。")

    def name_of(item: Mapping[str, Any]) -> str:
        settings = _profile_values(item)
        return str(
            item.get("uniqueModernGamertag")
            or item.get("modernGamertag")
            or item.get("gamertag")
            or settings.get("gamertag")
            or requested
        )

    exact = next((item for item in candidates if name_of(item).casefold() == requested.casefold()), None)
    item = exact or candidates[0]
    settings = _profile_values(item)
    xuid = str(item.get("xuid") or item.get("id") or item.get("hostId") or "").strip()
    if not xuid:
        raise XboxError("没有找到这个 Xbox 玩家代号，请检查后重新输入。")
    avatar = (
        item.get("profilePicture")
        or item.get("displayPicRaw")
        or item.get("avatar")
        or settings.get("gamedisplaypicraw")
        or settings.get("displaypicraw")
    )
    return {"gamertag": name_of(item), "xuid": xuid, "avatar_url": str(avatar) if avatar else None}


def _presence_records(payload: Any) -> dict[str, Mapping[str, Any]]:
    payload = _content(payload)
    values: list[Mapping[str, Any]] = []
    if isinstance(payload, list):
        values = [item for item in payload if isinstance(item, Mapping)]
    elif isinstance(payload, Mapping):
        for key in ("people", "presences", "presence", "users"):
            child = payload.get(key)
            if isinstance(child, list):
                values.extend(item for item in child if isinstance(item, Mapping))
            elif isinstance(child, Mapping):
                values.append(child)
        if any(key in payload for key in ("xuid", "userId", "id", "devices")):
            values.append(payload)
        for key, child in payload.items():
            if str(key).isdigit() and isinstance(child, Mapping):
                values.append({"xuid": str(key), **child})
    records: dict[str, Mapping[str, Any]] = {}
    for item in values:
        xuid = str(item.get("xuid") or item.get("userId") or item.get("id") or "").strip()
        if xuid:
            records[xuid] = item
    return records


def _mapping_value(item: Mapping[str, Any], *names: str) -> Any:
    folded = {str(key).casefold(): value for key, value in item.items()}
    return next((folded[name.casefold()] for name in names if name.casefold() in folded), None)


def _people_presence_records(payload: Any) -> dict[str, Mapping[str, Any]]:
    """Convert PeopleHub's explicit isGame presence details to our common shape."""
    payload = _content(payload)
    people = payload.get("people") if isinstance(payload, Mapping) else None
    if not isinstance(people, list):
        return {}
    records: dict[str, Mapping[str, Any]] = {}
    for person in people:
        if not isinstance(person, Mapping):
            continue
        xuid = str(_mapping_value(person, "xuid", "id") or "").strip()
        if not xuid:
            continue
        title_presence = _mapping_value(person, "titlePresence")
        title_presence = title_presence if isinstance(title_presence, Mapping) else {}
        primary_title_id = str(_mapping_value(title_presence, "titleId") or "")
        primary_title_name = str(_mapping_value(title_presence, "titleName") or "").strip()
        titles: list[dict[str, Any]] = []
        details = _mapping_value(person, "presenceDetails")
        if isinstance(details, list):
            for detail in details:
                if not isinstance(detail, Mapping) or _mapping_value(detail, "isGame") is not True:
                    continue
                title_id = str(_mapping_value(detail, "titleId") or "")
                name = (
                    primary_title_name
                    if primary_title_name and (not primary_title_id or primary_title_id == title_id)
                    else str(_mapping_value(detail, "presenceText", "richPresenceText") or "").strip()
                )
                titles.append({
                    "id": title_id,
                    "name": name,
                    "state": _mapping_value(detail, "state") or "Active",
                    "placement": "Full",
                    "isGame": True,
                })
        records[xuid] = {
            "xuid": xuid,
            "state": _mapping_value(person, "presenceState") or "Offline",
            "devices": [{"type": "PeopleHub", "titles": titles}],
        }
    return records


def _first_url(value: Any) -> str | None:
    preferred = ("imageUrl", "imageUri", "gameImageUrl", "displayImage", "boxArt", "tileImage", "url")
    if isinstance(value, Mapping):
        for key in preferred:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.startswith(("https://", "http://")):
                return candidate
        for key, child in value.items():
            if "logo" in str(key).casefold():
                continue
            found = _first_url(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _first_url(child)
            if found:
                return found
    return None


def _active_game(presence: Mapping[str, Any]) -> Mapping[str, Any] | None:
    titles: list[Mapping[str, Any]] = []
    devices = presence.get("devices")
    if isinstance(devices, list):
        for device in devices:
            if isinstance(device, Mapping) and isinstance(device.get("titles"), list):
                titles.extend(title for title in device["titles"] if isinstance(title, Mapping))
    direct_titles = presence.get("titles")
    if isinstance(direct_titles, list):
        titles.extend(title for title in direct_titles if isinstance(title, Mapping))

    def active(title: Mapping[str, Any]) -> bool:
        name = str(title.get("name") or title.get("titleName") or "").strip()
        state = str(title.get("state") or "active").casefold()
        placement = str(title.get("placement") or "full").casefold()
        is_game = title.get("isGame")
        return (
            bool(name)
            and name.casefold() not in SYSTEM_TITLES
            and is_game is not False
            and state in {"active", "playing"}
            and placement != "background"
        )

    return next((title for title in titles if active(title)), None)


def _stable_game_name(value: Any) -> str | None:
    """Remove Xbox rich-presence text appended to the stable title name."""
    name = " ".join(str(value or "").strip().split())
    if not name:
        return None
    # Some Xbox presence responses flatten the title and the changing activity
    # into one value, for example "Forza Horizon 6 - Hot Lapping at ...".
    # Only the part before the separator identifies the game.
    for separator in (" - ", " – ", " — "):
        title, found, _status = name.partition(separator)
        if found and title.strip():
            return title.strip()
    return name


def parse_xbox_activity(item: "XboxSubscription", presence: Mapping[str, Any]) -> SwitchActivity:
    game = _active_game(presence)
    name = _stable_game_name(game.get("name") or game.get("titleName")) if game else None
    state = str(presence.get("state") or presence.get("presenceState") or "Offline").upper()
    return SwitchActivity(
        account_name=item.gamertag,
        state="PLAYING" if game else state,
        game_name=name,
        avatar_url=item.avatar_url,
        game_image_url=_first_url(game) if game else None,
        platform="Xbox",
    )


@dataclass(frozen=True)
class XboxSubscription:
    gamertag: str
    xuid: str
    avatar_url: str | None
    qq_user_id: str
    group_id: str
    status: str = "active"
    current_game: str | None = None
    initialised: bool = False
    force_notify: bool = False
    nickname: str | None = None
    current_game_image_url: str | None = None


class XboxRegistry:
    def __init__(self, path: Path = DEFAULT_DB_PATH) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            db.execute(
                """CREATE TABLE IF NOT EXISTS xbox_subscriptions (
                gamertag TEXT NOT NULL, xuid TEXT NOT NULL, avatar_url TEXT,
                qq_user_id TEXT NOT NULL, group_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active', current_game TEXT,
                initialised INTEGER NOT NULL DEFAULT 0,
                force_notify INTEGER NOT NULL DEFAULT 0, nickname TEXT,
                current_game_image_url TEXT, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, PRIMARY KEY (xuid, group_id))"""
            )

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        return db

    @staticmethod
    def _row(row: sqlite3.Row) -> XboxSubscription:
        return XboxSubscription(
            gamertag=row["gamertag"], xuid=row["xuid"], avatar_url=row["avatar_url"],
            qq_user_id=row["qq_user_id"], group_id=row["group_id"], status=row["status"],
            current_game=row["current_game"], initialised=bool(row["initialised"]),
            force_notify=bool(row["force_notify"]), nickname=row["nickname"],
            current_game_image_url=row["current_game_image_url"],
        )

    def list_group(self, group_id: str) -> list[XboxSubscription]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM xbox_subscriptions WHERE group_id=? ORDER BY created_at", (group_id,)).fetchall()
        return [self._row(row) for row in rows]

    def list_all(self) -> list[XboxSubscription]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM xbox_subscriptions ORDER BY created_at").fetchall()
        return [self._row(row) for row in rows]

    def add(self, item: XboxSubscription) -> None:
        now = utc_now()
        with self._connect() as db:
            db.execute(
                """INSERT INTO xbox_subscriptions
                (gamertag,xuid,avatar_url,qq_user_id,group_id,status,current_game,initialised,
                 force_notify,nickname,current_game_image_url,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (item.gamertag,item.xuid,item.avatar_url,item.qq_user_id,item.group_id,item.status,
                 item.current_game,int(item.initialised),int(item.force_notify),item.nickname,
                 item.current_game_image_url,now,now),
            )

    def remove(self, gamertag: str, group_id: str, requester_id: str) -> bool:
        with self._connect() as db:
            result = db.execute("DELETE FROM xbox_subscriptions WHERE lower(gamertag)=lower(?) AND group_id=?", (gamertag, group_id))
        return result.rowcount > 0

    def set_nickname(self, gamertag: str, group_id: str, requester_id: str, nickname: str) -> bool:
        with self._connect() as db:
            result = db.execute("UPDATE xbox_subscriptions SET nickname=?,updated_at=? WHERE lower(gamertag)=lower(?) AND group_id=?", (nickname, utc_now(), gamertag, group_id))
        return result.rowcount > 0

    def update_presence(self, item: XboxSubscription, activity: SwitchActivity, *, initialised: bool) -> None:
        with self._connect() as db:
            db.execute(
                """UPDATE xbox_subscriptions SET avatar_url=?,status=?,current_game=?,
                current_game_image_url=?,initialised=?,force_notify=0,updated_at=?
                WHERE xuid=? AND group_id=?""",
                (activity.avatar_url or item.avatar_url, activity.state.casefold(),
                 activity.game_name if activity.game_key else None,
                 activity.game_image_url if activity.game_key else None,
                 int(initialised), utc_now(), item.xuid, item.group_id),
            )


class XboxClient:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        return self.direct_configured or self.openxbl_configured

    @property
    def direct_configured(self) -> bool:
        try:
            import xbox.webapi  # noqa: F401
            return _token_path().is_file() and _token_path().stat().st_size > 32
        except (ImportError, OSError):
            return False

    @property
    def openxbl_configured(self) -> bool:
        try:
            return bool(_credential_path().read_text(encoding="utf-8").strip())
        except OSError:
            return False

    @property
    def provider_name(self) -> str:
        if self.direct_configured:
            return "微软账号直连"
        if self.openxbl_configured:
            return "OpenXBL 备用"
        return "未登录"

    def _key(self) -> str:
        try:
            value = _credential_path().read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise XboxError("Xbox API Key 尚未配置，请联系机器人管理员。") from exc
        if not value:
            raise XboxError("Xbox API Key 尚未配置，请联系机器人管理员。")
        return value

    async def _openxbl_get(self, path: str) -> Any:
        headers = {"X-Authorization": self._key(), "Accept": "application/json"}
        async with self._lock:
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(15), follow_redirects=True) as client:
                    response = await client.get(f"{API_ROOT}/{path.lstrip('/')}", headers=headers)
                response.raise_for_status()
                return response.json()
            except XboxError:
                raise
            except Exception as exc:
                logger.exception("Xbox API request failed: %s", path)
                raise XboxError(xbox_user_message(exc)) from exc

    async def _direct_call(self, operation: str, callback: Any) -> Any:
        try:
            from xbox.webapi.api.client import XboxLiveClient
            from xbox.webapi.authentication.manager import AuthenticationManager
            from xbox.webapi.authentication.models import OAuth2TokenResponse
            from xbox.webapi.common.signed_session import SignedSession
        except ImportError as exc:
            raise XboxError("Xbox 直连组件尚未安装，请重新运行笔记本安装脚本。") from exc
        token_path = _token_path()
        lock_acquired = False
        try:
            await self._lock.acquire()
            lock_acquired = True
            async with AsyncInterProcessFileLock(lock_path_for(token_path)):
                try:
                    # Read only after both the in-process and cross-process
                    # locks are held. Otherwise a concurrent command could
                    # queue behind a refresh while retaining a stale token
                    # snapshot in memory.
                    token_json = token_path.read_text(encoding="utf-8")
                except OSError as exc:
                    raise XboxError("Xbox 观察账号尚未登录，请在管理后台打开 Xbox 登录。") from exc
                try:
                    async with SignedSession() as session:
                        manager = AuthenticationManager(
                            session,
                            os.getenv("XBOX_CLIENT_ID", DEFAULT_CLIENT_ID).strip() or DEFAULT_CLIENT_ID,
                            os.getenv("XBOX_CLIENT_SECRET", "").strip(),
                            os.getenv("XBOX_REDIRECT_URI", DEFAULT_REDIRECT_URI).strip() or DEFAULT_REDIRECT_URI,
                        )
                        manager.oauth = OAuth2TokenResponse.model_validate_json(token_json)
                        await manager.refresh_tokens()
                        # Microsoft may rotate the refresh token. Persist the
                        # refreshed OAuth response before any business request
                        # so a transient API failure cannot discard it.
                        if manager.oauth is None:
                            raise XboxError("Xbox 观察账号登录已失效，请在管理后台重新打开 Xbox 登录。")
                        atomic_write_text(token_path, manager.oauth.model_dump_json())
                        return await callback(XboxLiveClient(manager))
                except XboxError:
                    raise
                except Exception as exc:
                    detail = f"{type(exc).__name__}: {exc}".casefold()
                    logger.warning("Xbox direct API %s failed: %s", operation, type(exc).__name__)
                    if any(marker in detail for marker in ("401", "invalid_grant", "authenticationexception", "refresh token")):
                        raise XboxError("Xbox 观察账号登录已失效，请在管理后台重新打开 Xbox 登录。") from exc
                    raise XboxError(xbox_user_message(exc)) from exc
        except TimeoutError as exc:
            raise XboxError(xbox_user_message(exc)) from exc
        finally:
            if lock_acquired:
                self._lock.release()

    @staticmethod
    def _dump(value: Any) -> Any:
        if hasattr(value, "model_dump"):
            return value.model_dump(by_alias=True)
        return value

    async def _with_fallback(self, operation: str, direct: Any, fallback: Any) -> Any:
        if self.direct_configured:
            try:
                return await self._direct_call(operation, direct)
            except XboxError:
                if not self.openxbl_configured:
                    raise
                logger.warning("Xbox direct %s failed; trying OpenXBL fallback", operation)
        if self.openxbl_configured:
            return await fallback()
        raise XboxError("Xbox 观察账号尚未登录，请在管理后台打开 Xbox 登录。")

    async def status(self) -> None:
        async def direct(client: Any) -> None:
            await client.presence.get_presence_own()

        await self._with_fallback("status", direct, lambda: self._openxbl_get("account"))

    async def lookup(self, gamertag: str) -> dict[str, str | None]:
        async def direct(client: Any) -> dict[str, str | None]:
            profile_payload = self._dump(await client.profile.get_profile_by_gamertag(gamertag))
            profile = parse_xbox_profile(profile_payload, gamertag)
            presence_payload = [
                self._dump(item)
                for item in await client.presence.get_presence_batch(
                    [str(profile["xuid"])], presence_level="all"
                )
            ]
            if str(profile["xuid"]) not in _presence_records(presence_payload):
                raise XboxVisibilityError("请把 Xbox 的在线状态和当前游戏设为所有人可见")
            return profile

        async def fallback() -> dict[str, str | None]:
            try:
                payload = await self._openxbl_get(f"player/gamertag/{quote(gamertag, safe='')}")
            except XboxError as first:
                if "没有找到" not in str(first) and "暂时无法" not in str(first):
                    raise
                payload = await self._openxbl_get(f"search/{quote(gamertag, safe='')}")
            profile = parse_xbox_profile(payload, gamertag)
            presence_payload = await self._openxbl_get(f"{profile['xuid']}/presence")
            if str(profile["xuid"]) not in _presence_records(presence_payload):
                raise XboxVisibilityError("请把 Xbox 的在线状态和当前游戏设为所有人可见")
            return profile

        return await self._with_fallback("lookup", direct, fallback)

    async def presences(self, xuids: list[str]) -> dict[str, Mapping[str, Any]]:
        if not xuids:
            return {}
        async def direct(client: Any) -> dict[str, Mapping[str, Any]]:
            try:
                people = self._dump(await client.people.get_friends_own_batch(
                    xuids, decoration_fields=["presenceDetail", "titlePresence"]
                ))
                people_records = _people_presence_records(people)
                if people_records:
                    return people_records
            except Exception as exc:
                logger.debug("Xbox PeopleHub presence unavailable: %s", type(exc).__name__)
            return _presence_records([
                self._dump(item)
                for item in await client.presence.get_presence_batch(xuids, presence_level="all")
            ])

        async def fallback() -> dict[str, Mapping[str, Any]]:
            return _presence_records(await self._openxbl_get(f"{','.join(xuids)}/presence"))

        return await self._with_fallback("presence", direct, fallback)

    async def game_cover(self, title_id: str | None) -> str | None:
        if not title_id or not self.direct_configured:
            return None

        async def direct(client: Any) -> str | None:
            return _first_url(self._dump(await client.titlehub.get_title_info(str(title_id))))

        try:
            return await self._direct_call("title-cover", direct)
        except XboxError:
            logger.debug("Xbox title cover unavailable for %s", title_id)
            return None


def _should_notify(item: XboxSubscription, current_game: str | None) -> bool:
    if current_game is None:
        return False
    if item.force_notify or not item.initialised:
        return True
    previous_name = _stable_game_name(item.current_game)
    previous = previous_name.casefold() if previous_name else None
    return current_game != previous


class XboxPresenceMonitor:
    def __init__(self, registry: XboxRegistry, client: XboxClient) -> None:
        self.registry = registry
        self.client = client
        self.interval = max(float(os.getenv("XBOX_POLL_INTERVAL", "90")), 90)
        self._stop = asyncio.Event()
        self._presence_summaries: dict[str, str] = {}

    async def stop(self) -> None:
        self._stop.set()

    async def poll_once(self) -> None:
        subscriptions = await asyncio.to_thread(self.registry.list_all)
        if not subscriptions or not self.client.configured:
            return
        xuids = list(dict.fromkeys(item.xuid for item in subscriptions))
        records: dict[str, Mapping[str, Any]] = {}
        for start in range(0, len(xuids), 50):
            if start:
                await asyncio.sleep(
                    max(float(os.getenv("XBOX_BATCH_DELAY_SECONDS", "0.25")), 0.0)
                )
            records.update(await self.client.presences(xuids[start:start + 50]))
        polled_at = datetime.now(timezone.utc)
        bots = [bot for bot in nonebot.get_bots().values() if isinstance(bot, Bot)]
        for item in subscriptions:
            presence = records.get(item.xuid)
            if presence is None:
                continue
            devices = presence.get("devices") if isinstance(presence, Mapping) else None
            summary_parts: list[str] = []
            if isinstance(devices, list):
                for device in devices:
                    if not isinstance(device, Mapping):
                        continue
                    names = [
                        str(title.get("name") or title.get("titleName") or "").strip()
                        for title in (device.get("titles") or [])
                        if isinstance(title, Mapping)
                    ]
                    summary_parts.append(f"{device.get('type') or 'Unknown'}: {', '.join(filter(None, names)) or '无标题'}")
            summary = "；".join(summary_parts) or f"状态={presence.get('state') or '未知'}，无设备标题"
            if self._presence_summaries.get(item.xuid) != summary:
                logger.info("Xbox Presence %s: %s", item.gamertag, summary)
                self._presence_summaries[item.xuid] = summary
            activity = parse_xbox_activity(item, presence)
            game = _active_game(presence)
            if activity.game_key and not activity.game_image_url and game:
                cover = await self.client.game_cover(str(game.get("id") or ""))
                if cover:
                    activity = replace(activity, game_image_url=cover)
            image_url = activity.game_image_url or (
                item.current_game_image_url
                if item.current_game and activity.game_key == item.current_game.casefold()
                else None
            )
            await asyncio.to_thread(
                game_time_tracker.observe, platform="xbox", account_id=item.xuid,
                group_id=item.group_id, display_name=item.nickname or item.gamertag,
                avatar_url=activity.avatar_url or item.avatar_url, game_key=activity.game_key,
                game_name=activity.game_name, game_image_url=image_url, observed_at=polled_at,
            )
            should_notify = _should_notify(item, activity.game_key)
            sent = False
            if should_notify and bots:
                try:
                    message = await asyncio.wait_for(build_activity_message(item.nickname or item.gamertag, activity), timeout=15)
                    await asyncio.wait_for(bots[0].send_group_msg(group_id=int(item.group_id), message=message), timeout=15)
                    sent = True
                    logger.info("Sent Xbox activity for %s to group %s", item.gamertag, item.group_id)
                except TimeoutError:
                    logger.warning("Xbox activity notification timed out for %s", item.gamertag)
                except Exception:
                    logger.exception("Failed to send Xbox activity for %s to group %s", item.gamertag, item.group_id)
            if not should_notify or sent:
                await asyncio.to_thread(self.registry.update_presence, item, activity, initialised=True)
        await asyncio.to_thread(game_time_tracker.mark_poll, "xbox", polled_at)

    async def run(self) -> None:
        backoff = FailureBackoff(self.interval, 900.0)
        while not self._stop.is_set():
            if not self.client.configured:
                mark_failure("xbox", "观察账号尚未登录")
                await wait_for_stop(self._stop, 300.0)
                continue
            try:
                await self.poll_once()
                mark_success("xbox", f"监控正常（{self.client.provider_name}）")
                backoff.success()
                delay = self.interval
            except XboxError as exc:
                mark_failure("xbox", str(exc))
                delay = backoff.failure()
                if backoff.should_log():
                    logger.warning("Xbox monitoring unavailable: %s; retry in %.0fs", exc, delay)
            except Exception as exc:
                mark_failure("xbox", "内部异常，正在自动恢复")
                delay = backoff.failure()
                if backoff.should_log():
                    logger.error("Unexpected Xbox monitoring failure: %s; retry in %.0fs", type(exc).__name__, delay, exc_info=True)
            await wait_for_stop(self._stop, delay)


registry = XboxRegistry(Path(os.getenv("XBOX_DB_PATH", DEFAULT_DB_PATH)))
xbox_client = XboxClient()
_monitor: XboxPresenceMonitor | None = None
_task: asyncio.Task[None] | None = None


async def start_xbox_monitor() -> None:
    global _monitor, _task
    _monitor = XboxPresenceMonitor(registry, xbox_client)
    _task = asyncio.create_task(supervise("xbox-presence-monitor", _monitor.run, _monitor._stop.is_set), name="xbox-presence-monitor-supervisor")
    logger.info("Xbox presence monitor ready (configured=%s)", xbox_client.configured)


async def stop_xbox_monitor() -> None:
    global _monitor, _task
    if _monitor:
        await _monitor.stop()
    if _task:
        await _task
    _monitor = None
    _task = None

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import nonebot
from nonebot.adapters.onebot.v11 import Bot

from .game_time_tracker import tracker as game_time_tracker
from .health import mark_failure, mark_success
from .resilience import FailureBackoff, supervise, wait_for_stop
from .switch_presence import SwitchActivity, build_activity_message, parse_switch_activity


logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "switch_registry.db"
FRIEND_CODE_RE = re.compile(r"^(?:SW[-\s]?)?(\d{4})[-\s]?(\d{4})[-\s]?(\d{4})$", re.IGNORECASE)
FRIEND_REQUEST_PENDING_MESSAGE = "好友申请已经发送，请等待对方同意。"


class NxapiError(RuntimeError):
    pass


class NxapiTransientError(NxapiError):
    """A safe-to-retry nxapi transport or upstream service failure."""


class NxapiLoginUnavailableError(NxapiError):
    """NSO login is disabled, version-rejected, or temporarily rate limited."""

    def __init__(self, message: str, retry_after: float) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def _nxapi_login_pause_seconds(detail: str) -> float | None:
    lowered = detail.casefold()
    if "too many attempts to authenticate" in lowered:
        return 3600.0
    if "9427" in detail or "upgrade required" in lowered:
        return 1800.0
    if "remote configuration prevents coral authentication" in lowered:
        return 1800.0
    return None


def _is_transient_nxapi_error(detail: str) -> bool:
    lowered = detail.casefold()
    return any(
        marker in lowered
        for marker in (
            "fetch failed",
            "econnreset",
            "etimedout",
            "eai_again",
            "socket disconnected",
            "before secure tls connection",
            "502 bad gateway",
            "503 service unavailable",
            "504 gateway timeout",
            "bad gateway",
            "retry-after",
        )
    )


def nxapi_user_message(detail: str) -> str:
    """Convert nxapi/Nintendo diagnostics into text safe to send to a QQ group."""
    lowered = detail.casefold()
    if "9427" in detail or "upgrade required" in lowered:
        return "Switch 登录接口要求更新客户端版本，请管理员检查 nxapi 远程配置及上游兼容更新。"
    if "too many attempts to authenticate" in lowered:
        return "Switch 登录尝试过于频繁，请暂停重试，等待登录冷却后再试。"
    if "9402" in detail or "resource not found" in lowered:
        return "没有找到这个 Switch 好友码，请检查好友码或对方的好友申请设置。"
    if "9460" in detail or "invalid_friend_request" in lowered:
        return "无法发送好友申请，请检查好友码和双方的好友设置。"
    if "9461" in detail or "sender_friend_limit_exceeded" in lowered:
        return "观察账号的 Switch 好友数量已达上限。"
    if "9462" in detail or "receiver_friend_limit_exceeded" in lowered:
        return "对方的 Switch 好友数量已达上限。"
    if "9463" in detail or "friend_request_not_accepted" in lowered:
        return "对方目前不接受好友申请。"
    if "9464" in detail or "duplicate_friend_request" in lowered:
        return FRIEND_REQUEST_PENDING_MESSAGE
    if "9467" in detail or "already_friend" in lowered:
        return "这个账号已经是观察账号的 Switch 好友。"
    if "9468" in detail or "sender_blocks_receiver_friend_request" in lowered:
        return "双方的好友设置或屏蔽关系阻止了好友申请。"
    if "9437" in detail or "rate_limit_exceeded" in lowered:
        return "操作过于频繁，请稍后再试。"
    if any(value in lowered for value in ("eperm", "ebusy", "resource busy", "file is locked")):
        return "Switch 服务正在处理其他请求，请稍后再试。"
    if _is_transient_nxapi_error(detail):
        return "Switch 网络连接暂时不稳定，请稍后再试。"
    if any(value in detail for value in ("9501", "9511")) or any(
        value in lowered for value in ("service_unavailable", "maintenance")
    ):
        return "Nintendo 服务暂时不可用，请稍后再试。"
    if "remote configuration prevents coral authentication" in lowered:
        return "nxapi 上游配置不允许当前客户端登录，请管理员检查 nxapi 兼容更新或上游状态；机器人会自动重试。"
    if any(value in detail for value in ("SelectedUser", "NintendoAccountToken")) or (
        "token" in lowered and any(value in lowered for value in ("missing", "invalid", "expired"))
    ):
        return "Switch 观察账号尚未登录或登录已失效，请联系机器人管理员。"
    return "Switch 服务暂时无法完成操作，请稍后再试。"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_friend_code(value: str) -> str:
    cleaned = value.strip().replace("—", "-").replace("–", "-").replace("－", "-")
    match = FRIEND_CODE_RE.fullmatch(cleaned)
    if not match:
        raise ValueError("好友码格式应为 SW-1234-5678-9012")
    return "-".join(match.groups())


@dataclass(frozen=True)
class SwitchSubscription:
    friend_code: str
    nsa_id: str
    ns_name: str
    avatar_url: str | None
    qq_user_id: str
    group_id: str
    status: str
    current_game: str | None
    initialised: bool
    force_notify: bool = False
    nickname: str | None = None
    current_game_image_url: str | None = None


class SwitchRegistry:
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
                CREATE TABLE IF NOT EXISTS switch_subscriptions (
                    friend_code TEXT NOT NULL,
                    nsa_id TEXT NOT NULL,
                    ns_name TEXT NOT NULL,
                    avatar_url TEXT,
                    qq_user_id TEXT NOT NULL,
                    group_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    current_game TEXT,
                    initialised INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (friend_code, group_id)
                )
                """
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(switch_subscriptions)")}
            if "force_notify" not in columns:
                db.execute(
                    "ALTER TABLE switch_subscriptions ADD COLUMN force_notify INTEGER NOT NULL DEFAULT 0"
                )
            if "nickname" not in columns:
                db.execute("ALTER TABLE switch_subscriptions ADD COLUMN nickname TEXT")
            if "current_game_image_url" not in columns:
                db.execute(
                    "ALTER TABLE switch_subscriptions ADD COLUMN current_game_image_url TEXT"
                )

    @staticmethod
    def _row(row: sqlite3.Row) -> SwitchSubscription:
        return SwitchSubscription(
            friend_code=row["friend_code"], nsa_id=row["nsa_id"], ns_name=row["ns_name"],
            avatar_url=row["avatar_url"], qq_user_id=row["qq_user_id"], group_id=row["group_id"],
            status=row["status"], current_game=row["current_game"], initialised=bool(row["initialised"]),
            force_notify=bool(row["force_notify"]),
            nickname=row["nickname"],
            current_game_image_url=row["current_game_image_url"],
        )

    def list_group(self, group_id: str) -> list[SwitchSubscription]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM switch_subscriptions WHERE group_id = ? ORDER BY created_at", (group_id,)
            ).fetchall()
        return [self._row(row) for row in rows]

    def list_all(self) -> list[SwitchSubscription]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM switch_subscriptions ORDER BY created_at").fetchall()
        return [self._row(row) for row in rows]

    def add(self, item: SwitchSubscription) -> None:
        now = utc_now()
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO switch_subscriptions
                (friend_code, nsa_id, ns_name, avatar_url, qq_user_id, group_id, status,
                 current_game, initialised, force_notify, nickname, current_game_image_url,
                 created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (item.friend_code, item.nsa_id, item.ns_name, item.avatar_url, item.qq_user_id,
                 item.group_id, item.status, item.current_game, int(item.initialised),
                 int(item.force_notify), item.nickname, item.current_game_image_url, now, now),
            )

    def remove(self, friend_code: str, group_id: str, requester_id: str) -> bool:
        with self._connect() as db:
            result = db.execute(
                "DELETE FROM switch_subscriptions WHERE friend_code = ? AND group_id = ?",
                (friend_code, group_id),
            )
        return result.rowcount > 0

    def set_nickname(
        self, friend_code: str, group_id: str, requester_id: str, nickname: str
    ) -> bool:
        with self._connect() as db:
            result = db.execute(
                "UPDATE switch_subscriptions SET nickname = ?, updated_at = ? "
                "WHERE friend_code = ? AND group_id = ?",
                (nickname, utc_now(), friend_code, group_id),
            )
        return result.rowcount > 0

    def update_presence(
        self, item: SwitchSubscription, activity: SwitchActivity, *, status: str, initialised: bool
    ) -> None:
        with self._connect() as db:
            db.execute(
                """
                UPDATE switch_subscriptions
                SET ns_name = ?, avatar_url = ?, status = ?, current_game = ?,
                    current_game_image_url = ?, initialised = ?,
                    force_notify = 0, updated_at = ?
                WHERE friend_code = ? AND group_id = ?
                """,
                (activity.account_name or item.ns_name, activity.avatar_url or item.avatar_url, status,
                 activity.game_name if activity.game_key else None,
                 activity.game_image_url if activity.game_key else None,
                 int(initialised), utc_now(),
                 item.friend_code, item.group_id),
            )

    def force_next_notification(self, friend_code: str, group_id: str) -> bool:
        with self._connect() as db:
            result = db.execute(
                "UPDATE switch_subscriptions SET force_notify = 1, updated_at = ? "
                "WHERE friend_code = ? AND group_id = ?",
                (utc_now(), friend_code, group_id),
            )
        return result.rowcount > 0


class NxapiClient:
    def __init__(self) -> None:
        self.command = self._resolve_command()
        # A wedged nxapi process must not freeze presence updates for minutes.
        self.timeout = max(float(os.getenv("SWITCH_NXAPI_TIMEOUT", "45")), 10)
        # nxapi uses one shared on-disk credential store. The background presence
        # poll and group commands must not launch CLI processes at the same time.
        self._lock = asyncio.Lock()

    @staticmethod
    def _resolve_command() -> list[str] | None:
        configured = os.getenv("NXAPI_COMMAND", "").strip()
        if configured:
            path = Path(configured)
            if path.suffix.lower() == ".js" and path.is_file():
                node = shutil.which("node")
                return [node, str(path)] if node else None
            found = shutil.which(configured)
            return [found] if found else None
        appdata = os.getenv("APPDATA")
        localappdata = os.getenv("LOCALAPPDATA")
        node = shutil.which("node")
        if node:
            candidates: list[Path] = []
            if appdata:
                candidates.extend([
                    Path(appdata) / "npm" / "node_modules" / "nxapi" / "bin" / "nxapi.js",
                    Path(appdata) / "npm" / "node_modules" / "@samuel" / "nxapi" / "bin" / "nxapi.js",
                ])
            if localappdata:
                candidates.append(
                    Path(localappdata) / "Programs" / "nxapi-app" / "resources" / "app"
                    / "dist" / "bundle" / "cli-bundle.js"
                )
            script = next((candidate for candidate in candidates if candidate.is_file()), None)
            if script is not None:
                return [node, str(script)]
        direct = shutil.which("nxapi")
        if direct:
            return [direct]
        return None

    @property
    def available(self) -> bool:
        return bool(self.command)

    async def _run(self, *arguments: str, retries: int = 0) -> str:
        async with self._lock:
            for attempt in range(retries + 1):
                try:
                    return await self._run_unlocked(*arguments)
                except NxapiTransientError:
                    if attempt >= retries:
                        raise
                    delay = 2 if attempt == 0 else 5
                    logger.warning(
                        "nxapi transient failure for %s; retrying in %ss (%d/%d)",
                        arguments[0] if arguments else "command",
                        delay,
                        attempt + 1,
                        retries,
                    )
                    await asyncio.sleep(delay)
            raise AssertionError("unreachable")

    async def _run_unlocked(self, *arguments: str) -> str:
        if not self.command:
            raise NxapiError("Switch 功能尚未配置，请联系机器人管理员。")
        environment = os.environ.copy()
        environment.setdefault("NXAPI_SKIP_UPDATE_CHECK", "1")
        environment.setdefault("NODE_OPTIONS", "--use-system-ca")
        environment.setdefault(
            "NXAPI_USER_AGENT",
            os.getenv("NXAPI_USER_AGENT", "video-analysis-switch-monitor/0.1.0 (contact: project maintainers)"),
        )
        process = await asyncio.create_subprocess_exec(
            *self.command, "nso", *arguments,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=environment,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=self.timeout)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise NxapiTransientError("Switch 服务响应超时，请稍后再试。") from None
        output = stdout.decode("utf-8", errors="replace").strip()
        error = stderr.decode("utf-8", errors="replace").strip()
        if process.returncode != 0:
            detail = error or output or f"退出码 {process.returncode}"
            login_pause = _nxapi_login_pause_seconds(detail)
            if login_pause is not None:
                message = nxapi_user_message(detail)
                logger.warning(
                    "nxapi login unavailable: %s; authentication retry paused for %.0fs",
                    message,
                    login_pause,
                )
                raise NxapiLoginUnavailableError(message, login_pause)
            if _is_transient_nxapi_error(detail):
                logger.warning(
                    "nxapi transient command failure (arguments=%s, returncode=%s): %s",
                    arguments,
                    process.returncode,
                    nxapi_user_message(detail),
                )
                raise NxapiTransientError(nxapi_user_message(detail))
            logger.error(
                "nxapi command failed (arguments=%s, returncode=%s): %s",
                arguments,
                process.returncode,
                detail,
            )
            raise NxapiError(nxapi_user_message(detail))
        return output

    async def lookup(self, friend_code: str) -> Mapping[str, Any]:
        output = await self._run("lookup", friend_code, "--json", retries=2)
        try:
            data = json.loads(output)
        except json.JSONDecodeError as exc:
            logger.exception("nxapi lookup returned invalid JSON")
            raise NxapiError("Switch 服务返回了异常数据，请稍后再试。") from exc
        if not isinstance(data, Mapping) or not data.get("nsaId"):
            raise NxapiError("没有找到这个 Switch 好友码")
        return data

    async def add_friend(self, friend_code: str) -> str:
        return await self._run("add-friend", friend_code)

    async def friends(self, *, retries: int = 0) -> list[Mapping[str, Any]]:
        output = await self._run("friends", "--json", retries=retries)
        try:
            data = json.loads(output)
        except json.JSONDecodeError as exc:
            logger.exception("nxapi friends returned invalid JSON")
            raise NxapiError("Switch 服务返回了异常数据，请稍后再试。") from exc
        if not isinstance(data, list):
            logger.error("nxapi friends returned unexpected data type: %s", type(data).__name__)
            raise NxapiError("Switch 服务返回了异常数据，请稍后再试。")
        return [item for item in data if isinstance(item, Mapping)]


def _should_notify(item: SwitchSubscription, current_game: str | None) -> bool:
    if current_game is None:
        return False
    if item.force_notify or not item.initialised:
        return True
    previous_game = item.current_game.casefold() if item.current_game else None
    return current_game != previous_game


class NxapiFriendMonitor:
    def __init__(self, registry: SwitchRegistry, client: NxapiClient) -> None:
        self.registry = registry
        self.client = client
        self.interval = max(float(os.getenv("SWITCH_POLL_INTERVAL", "60")), 30)
        self._stop = asyncio.Event()

    async def stop(self) -> None:
        self._stop.set()

    async def poll_once(self) -> None:
        subscriptions = await asyncio.to_thread(self.registry.list_all)
        if not subscriptions:
            return
        friends = await self.client.friends()
        polled_at = datetime.now(timezone.utc)
        by_nsa_id = {str(item.get("nsaId")): item for item in friends if item.get("nsaId")}
        bots = [bot for bot in nonebot.get_bots().values() if isinstance(bot, Bot)]

        for item in subscriptions:
            friend = by_nsa_id.get(item.nsa_id)
            if friend is None:
                continue
            activity = parse_switch_activity(friend)
            current_game = activity.game_key
            display_name = item.nickname or activity.account_name or item.ns_name
            await asyncio.to_thread(
                game_time_tracker.observe,
                platform="switch",
                account_id=item.friend_code,
                group_id=item.group_id,
                display_name=display_name,
                avatar_url=activity.avatar_url or item.avatar_url,
                game_key=current_game,
                game_name=activity.game_name,
                game_image_url=activity.game_image_url,
                observed_at=polled_at,
            )
            should_notify = _should_notify(item, current_game)
            sent = False
            if should_notify and bots:
                try:
                    message = await asyncio.wait_for(
                        build_activity_message(display_name, activity), timeout=15
                    )
                    result = await asyncio.wait_for(
                        bots[0].send_group_msg(
                            group_id=int(item.group_id), message=message
                        ),
                        timeout=15,
                    )
                    sent = True
                    logger.info(
                        "Sent Switch activity card for %s to group %s: %s",
                        item.friend_code,
                        item.group_id,
                        result,
                    )
                except TimeoutError:
                    logger.warning(
                        "Switch activity notification timed out for %s in group %s",
                        item.friend_code,
                        item.group_id,
                    )
                except Exception:
                    logger.exception(
                        "Failed to send Switch activity for %s to group %s",
                        item.friend_code,
                        item.group_id,
                    )
            # Presence storage must never depend on downloading a card or on a
            # timely OneBot response. Otherwise /switch list can remain stale
            # indefinitely while notification delivery is stuck.
            await asyncio.to_thread(
                self.registry.update_presence, item, activity, status="active", initialised=True
            )
            if should_notify and not sent:
                # Keep retrying the notification on a later poll while the list
                # already reflects the newest presence.
                await asyncio.to_thread(
                    self.registry.force_next_notification, item.friend_code, item.group_id
                )
        await asyncio.to_thread(game_time_tracker.mark_poll, "switch", polled_at)

    async def run(self) -> None:
        # Retry often enough that a short nxapi outage cannot leave cards an
        # hour behind. Calls themselves have a hard timeout above.
        backoff = FailureBackoff(self.interval, 120.0)
        while not self._stop.is_set():
            try:
                await self.poll_once()
                mark_success("switch", "监控正常")
                backoff.success()
                delay = self.interval
            except NxapiLoginUnavailableError as exc:
                mark_failure("switch", str(exc))
                delay = exc.retry_after
                logger.warning(
                    "Switch monitoring paused because nxapi login is unavailable: %s; retry in %.0fs",
                    exc,
                    delay,
                )
            except NxapiError as exc:
                mark_failure("switch", str(exc))
                delay = backoff.failure()
                if backoff.should_log():
                    logger.warning("Switch monitoring unavailable: %s; retry in %.0fs", exc, delay)
            except Exception as exc:
                mark_failure("switch", "内部异常，正在自动恢复")
                delay = backoff.failure()
                if backoff.should_log():
                    logger.error(
                        "Unexpected Switch monitoring failure: %s; retry in %.0fs",
                        type(exc).__name__,
                        delay,
                        exc_info=True,
                    )
            await wait_for_stop(self._stop, delay)


registry = SwitchRegistry(Path(os.getenv("SWITCH_DB_PATH", DEFAULT_DB_PATH)))
nxapi_client = NxapiClient()
_monitor: NxapiFriendMonitor | None = None
_task: asyncio.Task[None] | None = None


async def start_nxapi_friend_monitor() -> None:
    global _monitor, _task
    _monitor = NxapiFriendMonitor(registry, nxapi_client)
    if not nxapi_client.available:
        mark_failure("switch", "nxapi 未安装")
    _task = asyncio.create_task(
        supervise("nxapi-friend-monitor", _monitor.run, _monitor._stop.is_set),
        name="nxapi-friend-monitor-supervisor",
    )
    logger.info("Switch friend-code monitor ready (nxapi=%s)", nxapi_client.available)


async def stop_nxapi_friend_monitor() -> None:
    global _monitor, _task
    if _monitor:
        await _monitor.stop()
    if _task:
        await _task
    _monitor = None
    _task = None

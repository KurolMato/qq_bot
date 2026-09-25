from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
from io import BytesIO
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import time
from datetime import date, datetime
from pathlib import Path
from threading import RLock
from typing import Annotated

from fastapi import Cookie, Depends, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import BaseModel, Field

from qq_bot.game_calendar import LOCAL_IMAGE_DIR, CalendarGame, GameCalendarRegistry


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"
TOKEN_PATH = PROJECT_ROOT / "secrets" / "admin-token.txt"
ADMIN_HTML_PATH = PROJECT_ROOT / "static" / "admin.html"
HEARTBEAT_PATH = PROJECT_ROOT / "data" / "qq-runtime-heartbeat.json"
BOT_LOG_PATH = PROJECT_ROOT / "data" / "qq-bot.log"
WATCHDOG_LOG_PATH = PROJECT_ROOT / "data" / "napcat-watchdog.log"
ADMIN_ACTION_LOG_PATH = PROJECT_ROOT / "data" / "admin-actions.log"
CREATE_NEW_CONSOLE = 0x00000010
CREATE_NO_WINDOW = 0x08000000
GAME_CALENDAR_REGISTRY = GameCalendarRegistry()
GAME_PLATFORMS = {"Switch", "PS", "Steam", "Xbox", "PC", "Other"}
MAX_GAME_IMAGE_BYTES = 8 * 1024 * 1024

EDITABLE_FIELDS = {
    "QQ_ALLOWED_GROUPS",
    "NAPCAT_LAUNCHER",
    "NAPCAT_QQ",
    "QQ_MAX_VIDEO_MB",
    "SWITCH_POLL_INTERVAL",
    "PS5_POLL_INTERVAL",
    "STEAM_POLL_INTERVAL",
    "XBOX_POLL_INTERVAL",
    "GAME_TIME_MAX_SAMPLE_GAP",
    "GAME_RELEASE_REMINDER_HOUR",
    "GAME_RELEASE_REMINDER_DAYS",
    "GAME_RELEASE_REMINDER_INTERVAL",
    "DOUYIN_COOKIES_FILE",
    "DOUYIN_COOKIES_FROM_BROWSER",
    "XIAOHONGSHU_COOKIES_FILE",
    "XIAOHONGSHU_COOKIES_FROM_BROWSER",
}
CONFIG_DEFAULTS = {
    "QQ_ALLOWED_GROUPS": "",
    "NAPCAT_LAUNCHER": str(PROJECT_ROOT.parent / "NapCat.Shell" / "launcher-win10.bat"),
    "NAPCAT_QQ": "",
    "QQ_MAX_VIDEO_MB": "100",
    "SWITCH_POLL_INTERVAL": "60",
    "PS5_POLL_INTERVAL": "90",
    "STEAM_POLL_INTERVAL": "90",
    "XBOX_POLL_INTERVAL": "90",
    "GAME_TIME_MAX_SAMPLE_GAP": "300",
    "GAME_RELEASE_REMINDER_HOUR": "9",
    "GAME_RELEASE_REMINDER_DAYS": "1,0",
    "GAME_RELEASE_REMINDER_INTERVAL": "300",
    "DOUYIN_COOKIES_FILE": "",
    "DOUYIN_COOKIES_FROM_BROWSER": "",
    "XIAOHONGSHU_COOKIES_FILE": "",
    "XIAOHONGSHU_COOKIES_FROM_BROWSER": "",
}
TEXT_CONFIG_FIELDS = {
    "QQ_ALLOWED_GROUPS",
    "NAPCAT_LAUNCHER",
    "NAPCAT_QQ",
    "GAME_RELEASE_REMINDER_HOUR",
    "GAME_RELEASE_REMINDER_DAYS",
    "DOUYIN_COOKIES_FILE",
    "DOUYIN_COOKIES_FROM_BROWSER",
    "XIAOHONGSHU_COOKIES_FILE",
    "XIAOHONGSHU_COOKIES_FROM_BROWSER",
}
SECRET_FILES = {
    "steam": ("STEAM_API_KEY_FILE", "secrets/steam-api-key.txt"),
    "steamgriddb": ("STEAMGRIDDB_API_KEY_FILE", "secrets/steamgriddb-api-key.txt"),
    "psn": ("PSN_NPSSO_FILE", "secrets/psn-npsso.txt"),
    "openxbl": ("OPENXBL_API_KEY_FILE", "secrets/openxbl-api-key.txt"),
}
LOGURU_ENTRY_RE = re.compile(
    r"^(?P<time>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?)\s*\|\s*"
    r"(?P<level>TRACE|DEBUG|INFO|SUCCESS|WARNING|ERROR|CRITICAL)\s*\|\s*"
    r"(?P<context>.*?)\s+-\s(?P<message>.*)$"
)
STANDARD_ENTRY_RE = re.compile(
    r"^(?P<time>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:,\d+)?)\s+"
    r"(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+(?P<message>.*)$"
)
BOT_LOG_NOISE_MARKERS = (
    "[message.",
    "[notice.",
    "[request.",
    "[meta_event.",
    "event will be handled by matcher",
    "matcher(type=",
    "checking for matchers",
    "running handler dependent",
    "calling api get_msg",
    '"get /health ',
)


class ConfigUpdate(BaseModel):
    values: dict[str, str]


class SecretUpdate(BaseModel):
    value: str


class GameCalendarUpdate(BaseModel):
    game_id: str | None = None
    name: str = Field(min_length=1, max_length=120)
    release_date: date
    platform: str = Field(min_length=1, max_length=20)
    image_data: str | None = None


def _admin_token() -> str:
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        value = TOKEN_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        value = ""
    if len(value) < 32:
        value = secrets.token_urlsafe(32)
        temporary = TOKEN_PATH.with_suffix(".tmp")
        temporary.write_text(value + "\n", encoding="utf-8")
        temporary.replace(TOKEN_PATH)
    return value


ADMIN_TOKEN = _admin_token()
ADMIN_SESSION_TTL_SECONDS = 30 * 86400
_ADMIN_SESSIONS: dict[str, float] = {}
_ADMIN_SESSIONS_LOCK = RLock()


def _session_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _issue_admin_session() -> str:
    """Create a random, process-local session instead of reusing ADMIN_TOKEN."""
    value = secrets.token_urlsafe(32)
    now = time.time()
    with _ADMIN_SESSIONS_LOCK:
        # Prune expired sessions while issuing a new one so a long-running
        # admin service cannot accumulate abandoned session entries.
        for digest, expires_at in list(_ADMIN_SESSIONS.items()):
            if expires_at <= now:
                _ADMIN_SESSIONS.pop(digest, None)
        _ADMIN_SESSIONS[_session_digest(value)] = now + ADMIN_SESSION_TTL_SECONDS
    return value


def _valid_admin_session(value: str) -> bool:
    if not value:
        return False
    digest = _session_digest(value)
    now = time.time()
    with _ADMIN_SESSIONS_LOCK:
        expires_at = _ADMIN_SESSIONS.get(digest)
        if expires_at is None:
            return False
        if expires_at <= now:
            _ADMIN_SESSIONS.pop(digest, None)
            return False
        return True


def _loopback(request: Request) -> bool:
    return request.client is not None and request.client.host in {"127.0.0.1", "::1"}


def require_admin(
    request: Request,
    admin_session: Annotated[str | None, Cookie()] = None,
) -> None:
    if not _loopback(request) or not _valid_admin_session(admin_session or ""):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="管理后台认证失败")


def admin_page(request: Request, token: str | None = None) -> Response:
    if not _loopback(request):
        raise HTTPException(status_code=403, detail="管理后台只允许本机访问")
    if token:
        if not hmac.compare_digest(token, ADMIN_TOKEN):
            return HTMLResponse(
                "<meta charset='utf-8'><title>认证失败</title>"
                "<body style='font-family:system-ui;background:#08111a;color:#eef;padding:48px'>"
                "管理后台认证失败，请重新打开 <b>机器人管理.cmd</b> 并选择“打开管理后台”。</body>",
                status_code=403,
            )
        session = _issue_admin_session()
        response = RedirectResponse("/admin", status_code=303)
        response.set_cookie(
            "admin_session",
            session,
            httponly=True,
            samesite="strict",
            secure=False,
            max_age=ADMIN_SESSION_TTL_SECONDS,
        )
        # The redirect removes the one-time login token from the address bar
        # before the admin page is rendered and prevents it being sent as a
        # referrer to a later resource.
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response
    cookie = request.cookies.get("admin_session", "")
    if not _valid_admin_session(cookie):
        return HTMLResponse(
            "<meta charset='utf-8'><title>需要认证</title>"
            "<body style='font-family:system-ui;background:#08111a;color:#eef;padding:48px'>"
            "请打开项目根目录的 <b>机器人管理.cmd</b> 并选择“打开管理后台”。</body>",
            status_code=403,
        )
    return HTMLResponse(
        ADMIN_HTML_PATH.read_text(encoding="utf-8"),
        headers={
            "Cache-Control": "no-store",
            "X-Frame-Options": "DENY",
            "Content-Security-Policy": (
                "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                "script-src 'self' 'unsafe-inline'; connect-src 'self'; "
                "img-src 'self' https: data:; frame-ancestors 'none'"
            ),
        },
    )


def _read_env() -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        lines = ENV_PATH.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return result
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip()
    return result


def _write_env(updates: dict[str, str]) -> None:
    if any("\r" in value or "\n" in value for value in updates.values()):
        raise HTTPException(status_code=422, detail="配置值不能包含换行")
    try:
        lines = ENV_PATH.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        lines = []
    remaining = dict(updates)
    output: list[str] = []
    for line in lines:
        if "=" not in line or line.lstrip().startswith("#"):
            output.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key in remaining:
            output.append(f"{key}={remaining.pop(key)}")
        else:
            output.append(line)
    if remaining:
        if output and output[-1].strip():
            output.append("")
        output.extend(f"{key}={value}" for key, value in remaining.items())
    temporary = ENV_PATH.with_suffix(".tmp")
    temporary.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")
    temporary.replace(ENV_PATH)


def _validate_config(values: dict[str, str]) -> dict[str, str]:
    unknown = set(values) - EDITABLE_FIELDS
    if unknown:
        raise HTTPException(status_code=422, detail=f"不支持的配置项：{', '.join(sorted(unknown))}")
    cleaned = {key: str(value).strip() for key, value in values.items()}
    if any("\r" in value or "\n" in value for value in cleaned.values()):
        raise HTTPException(status_code=422, detail="配置值不能包含换行")
    groups = cleaned.get("QQ_ALLOWED_GROUPS", "")
    if groups and any(not item.strip().isdigit() for item in groups.split(",")):
        raise HTTPException(status_code=422, detail="群号必须是数字，多个群用英文逗号分隔")
    qq = cleaned.get("NAPCAT_QQ", "")
    if qq and not qq.isdigit():
        raise HTTPException(status_code=422, detail="NapCat 快速登录 QQ 必须是数字")
    launcher = cleaned.get("NAPCAT_LAUNCHER", "")
    if launcher and Path(launcher).suffix.casefold() not in {".bat", ".cmd"}:
        raise HTTPException(status_code=422, detail="NapCat 启动器必须是 .bat 或 .cmd 文件")
    reminder_hour = cleaned.get("GAME_RELEASE_REMINDER_HOUR", "")
    if reminder_hour:
        try:
            hour = int(reminder_hour)
        except ValueError:
            raise HTTPException(status_code=422, detail="发售提醒小时必须是整数") from None
        if not 0 <= hour <= 23:
            raise HTTPException(status_code=422, detail="发售提醒小时必须在 0 到 23 之间")
    reminder_days = cleaned.get("GAME_RELEASE_REMINDER_DAYS", "")
    if reminder_days:
        try:
            days = [int(item.strip()) for item in reminder_days.split(",") if item.strip()]
        except ValueError:
            raise HTTPException(status_code=422, detail="发售提醒天数必须用英文逗号分隔") from None
        if not days or any(day < 0 or day > 30 for day in days):
            raise HTTPException(status_code=422, detail="发售提醒天数必须在 0 到 30 之间")
    for key, platform_name in (
        ("DOUYIN_COOKIES_FROM_BROWSER", "抖音"),
        ("XIAOHONGSHU_COOKIES_FROM_BROWSER", "小红书"),
    ):
        browser = cleaned.get(key, "")
        if browser and not re.fullmatch(r"[A-Za-z0-9_:+.-]{2,80}", browser):
            raise HTTPException(status_code=422, detail=f"{platform_name}浏览器 Cookie 规格格式不正确")
    for key in EDITABLE_FIELDS - TEXT_CONFIG_FIELDS:
        if key in cleaned:
            try:
                number = int(cleaned[key])
            except ValueError:
                raise HTTPException(status_code=422, detail=f"{key} 必须是整数") from None
            if number <= 0:
                raise HTTPException(status_code=422, detail=f"{key} 必须大于 0")
    return cleaned


def _resolve_config_path(value: str, default: str) -> Path:
    path = Path(value or default).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.25):
            return True
    except OSError:
        return False


def _heartbeat() -> dict[str, object]:
    try:
        payload = json.loads(HEARTBEAT_PATH.read_text(encoding="utf-8"))
        age = max(0.0, time.time() - float(payload["updated_at"]))
        return {
            "fresh": age <= 20,
            "connected": payload.get("onebot_connected") is True,
            "age_seconds": round(age, 1),
            "pid": int(payload["pid"]),
            "components": payload.get("components", {}),
            "uptime": str(payload.get("uptime", "未知")),
        }
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return {
            "fresh": False,
            "connected": False,
            "age_seconds": None,
            "pid": None,
            "components": {},
            "uptime": "未知",
        }


def _nxapi_path() -> Path | None:
    candidates = [
        Path(os.getenv("LOCALAPPDATA", "")) / "Programs/nxapi-app/resources/app/dist/bundle/cli-bundle.js",
        Path(os.getenv("APPDATA", "")) / "npm/node_modules/@samuel/nxapi/bin/nxapi.js",
        Path(os.getenv("APPDATA", "")) / "npm/node_modules/nxapi/bin/nxapi.js",
    ]
    return next((path for path in candidates if path.is_file()), None)


def _tail_text(path: Path, max_bytes: int = 768 * 1024) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            content = handle.read()
    except OSError:
        return ""
    text = content.decode("utf-8", errors="replace")
    if size > max_bytes and "\n" in text:
        text = text.split("\n", 1)[1]
    return text


def _redact_log_text(value: str) -> str:
    value = re.sub(
        r"(?i)(npsso|session[_-]?token|authorization|api[_-]?key)(\s*[:=]\s*)\S+",
        r"\1\2[已隐藏]",
        value,
    )
    return re.sub(r"eyJ[A-Za-z0-9_.-]{40,}", "[令牌已隐藏]", value)


def _parse_log(path: Path, source: str) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    current: dict[str, str] | None = None

    def finish() -> None:
        nonlocal current
        if current is None:
            return
        level = current["level"]
        message = current["message"]
        normalized = message.casefold()
        visible = level not in {"TRACE", "DEBUG"}
        if source == "QQ Bot" and any(marker in normalized for marker in BOT_LOG_NOISE_MARKERS):
            visible = False
        if visible:
            current["message"] = _redact_log_text(message.strip())[-8000:]
            entries.append(current)
        current = None

    for raw_line in _tail_text(path).splitlines():
        match = LOGURU_ENTRY_RE.match(raw_line) or STANDARD_ENTRY_RE.match(raw_line)
        if match:
            finish()
            values = match.groupdict()
            current = {
                "timestamp": values["time"].replace(",", ".")[:23],
                "level": values["level"],
                "source": source,
                "message": values["message"],
            }
        elif current is not None:
            current["message"] += "\n" + raw_line
    finish()
    return entries


def _bot_log_paths() -> list[Path]:
    try:
        candidates = [BOT_LOG_PATH, *BOT_LOG_PATH.parent.glob(
            f"{BOT_LOG_PATH.stem}.*{BOT_LOG_PATH.suffix}"
        )]
        candidates = [path for path in candidates if path.is_file()]
        return sorted(candidates, key=lambda path: path.stat().st_mtime)[-4:]
    except OSError:
        return [BOT_LOG_PATH]


def admin_logs(
    _: Annotated[None, Depends(require_admin)],
    limit: int = 100,
) -> dict[str, object]:
    safe_limit = max(20, min(int(limit), 200))
    entries = [
        *(
            entry
            for path in _bot_log_paths()
            for entry in _parse_log(path, "QQ Bot")
        ),
        *_parse_log(WATCHDOG_LOG_PATH, "自动恢复"),
        *_parse_log(ADMIN_ACTION_LOG_PATH, "管理台"),
    ]
    entries.sort(key=lambda entry: entry["timestamp"])
    return {
        "entries": entries[-safe_limit:],
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def _write_admin_action(message: str, level: str = "INFO") -> None:
    ADMIN_ACTION_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]
    with ADMIN_ACTION_LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(f"{timestamp} {level} {message}\n")


def admin_status(_: Annotated[None, Depends(require_admin)]) -> dict[str, object]:
    env = _read_env()
    secrets_state: dict[str, bool] = {}
    for name, (env_name, default) in SECRET_FILES.items():
        path = _resolve_config_path(env.get(env_name, ""), default)
        secrets_state[name] = path.is_file() and path.stat().st_size > 8
    xbox_tokens = _resolve_config_path(env.get("XBOX_TOKENS_FILE", ""), "secrets/xbox-tokens.json")
    secrets_state["xbox_oauth"] = xbox_tokens.is_file() and xbox_tokens.stat().st_size > 32
    launcher_value = env.get("NAPCAT_LAUNCHER", "")
    napcat_launcher = (
        _resolve_config_path(launcher_value, launcher_value) if launcher_value else None
    )
    portable_launchers = (
        PROJECT_ROOT / "NapCat.Shell" / "launcher-win10.bat",
        PROJECT_ROOT.parent / "NapCat.Shell" / "launcher-win10.bat",
    )
    using_portable_launcher = False
    if not napcat_launcher or not napcat_launcher.is_file():
        for portable_launcher in portable_launchers:
            if portable_launcher.is_file():
                napcat_launcher = portable_launcher
                using_portable_launcher = True
                break
    scripts_dir = Path(os.sys.executable).resolve().parent
    dependencies = {
        "python": Path(os.sys.executable).is_file(),
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "node": bool(shutil.which("node")),
        "nxapi": _nxapi_path() is not None,
        "yutto": bool(shutil.which("yutto") or (scripts_dir / "yutto.exe").is_file()),
        "yt_dlp": bool(shutil.which("yt-dlp") or (scripts_dir / "yt-dlp.exe").is_file()),
        "napcat_launcher": bool(napcat_launcher and napcat_launcher.is_file()),
    }
    heartbeat = _heartbeat()
    components = heartbeat.get("components", {})
    if not isinstance(components, dict):
        components = {}

    def component(name: str) -> dict[str, object]:
        value = components.get(name, {})
        if not isinstance(value, dict):
            value = {}
        return {
            "state": str(value.get("state", "starting")),
            "detail": str(value.get("detail", "等待机器人状态")),
            "failures": int(value.get("failures", 0) or 0),
        }

    return {
        "project": {
            "root": str(PROJECT_ROOT),
            "drive": PROJECT_ROOT.drive,
            "portable": all(
                not Path(value).is_absolute()
                for key, value in env.items()
                if key.endswith("_FILE") and value
            ),
        },
        "services": {
            "napcat": _port_open(6099),
            "video_api": _port_open(8000),
            "qq_bot": _port_open(8081),
            "onebot": heartbeat,
        },
        "bot_status": {
            "components": {
                "qq": component("qq"),
                "video": component("video"),
                "switch": component("switch"),
                "ps5": component("ps5"),
                "steam": component("steam"),
                "xbox": component("xbox"),
            },
            "uptime": heartbeat.get("uptime", "未知"),
        },
        "credentials": secrets_state,
        "dependencies": dependencies,
        "config": {
            key: (
                str(napcat_launcher)
                if key == "NAPCAT_LAUNCHER" and using_portable_launcher
                else env.get(key, CONFIG_DEFAULTS[key])
            )
            for key in sorted(EDITABLE_FIELDS)
        },
        "restart_required": False,
    }


def update_config(
    request: ConfigUpdate,
    _: Annotated[None, Depends(require_admin)],
) -> dict[str, object]:
    cleaned = _validate_config(request.values)
    _write_env(cleaned)
    return {"status": "ok", "message": "配置已保存，重启机器人后生效。", "restart_required": True}


def save_secret(
    kind: str,
    request: SecretUpdate,
    _: Annotated[None, Depends(require_admin)],
) -> dict[str, object]:
    if kind not in SECRET_FILES:
        raise HTTPException(status_code=404, detail="未知凭据类型")
    value = request.value.strip()
    if kind == "steam" and not re.fullmatch(r"[A-Fa-f0-9]{32}", value):
        raise HTTPException(status_code=422, detail="Steam Web API Key 应为 32 位十六进制字符")
    if kind == "psn" and not re.fullmatch(r"[A-Za-z0-9_-]{64}", value):
        raise HTTPException(status_code=422, detail="PSN NPSSO 应为 64 位字符")
    if kind == "steamgriddb" and len(value) < 16:
        raise HTTPException(status_code=422, detail="SteamGridDB API Key 为空或过短")
    if kind == "openxbl" and len(value) < 16:
        raise HTTPException(status_code=422, detail="OpenXBL API Key 为空或过短")
    env_name, default = SECRET_FILES[kind]
    path = _resolve_config_path(_read_env().get(env_name, ""), default)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value + "\n", encoding="utf-8")
    temporary.replace(path)
    return {"status": "ok", "message": "凭据已安全保存；页面不会回显内容。", "restart_required": True}


def _local_game_image_path(value: str | None) -> Path | None:
    if not value or not value.startswith("local:"):
        return None
    filename = value.removeprefix("local:")
    if not filename or Path(filename).name != filename:
        return None
    path = LOCAL_IMAGE_DIR / filename
    try:
        return path if path.resolve().parent == LOCAL_IMAGE_DIR.resolve() else None
    except OSError:
        return None


def _save_game_image(game_id: str, image_data: str) -> str:
    try:
        header, encoded = image_data.split(",", 1)
        if not header.casefold().startswith("data:image/"):
            raise ValueError("not an image data URL")
        content = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise HTTPException(status_code=422, detail="上传的图片格式无效") from exc
    if not content or len(content) > MAX_GAME_IMAGE_BYTES:
        raise HTTPException(status_code=422, detail="游戏图片必须小于 8 MB")
    try:
        with Image.open(BytesIO(content)) as opened:
            if opened.width * opened.height > 50_000_000:
                raise ValueError("image dimensions too large")
            image = ImageOps.exif_transpose(opened).convert("RGB")
            image.thumbnail((1600, 1000), Image.Resampling.LANCZOS)
            LOCAL_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
            filename = f"{re.sub(r'[^A-Za-z0-9_-]', '_', game_id)}.jpg"
            path = LOCAL_IMAGE_DIR / filename
            temporary = path.with_suffix(".tmp")
            image.save(temporary, format="JPEG", quality=90, optimize=True)
            temporary.replace(path)
    except (OSError, ValueError, UnidentifiedImageError) as exc:
        raise HTTPException(status_code=422, detail="无法读取上传的游戏图片") from exc
    return f"local:{filename}"


def _game_calendar_payload(game: CalendarGame) -> dict[str, object]:
    local_image = _local_game_image_path(game.image_url)
    preview_url = (
        f"/api/admin/game-calendar/{game.app_id}/image"
        if local_image is not None
        else game.image_url
    )
    return {
        "game_id": game.app_id,
        "name": game.name,
        "release_date": game.release_date.isoformat(),
        "platform": game.platform,
        "has_image": bool(game.image_url),
        "local_image": local_image is not None,
        "preview_url": preview_url,
    }


def admin_game_calendar(
    _: Annotated[None, Depends(require_admin)],
) -> dict[str, object]:
    games = GAME_CALENDAR_REGISTRY.list_all()
    return {"items": [_game_calendar_payload(game) for game in games]}


def save_game_calendar_entry(
    request: GameCalendarUpdate,
    _: Annotated[None, Depends(require_admin)],
) -> dict[str, object]:
    platform_map = {value.casefold(): value for value in GAME_PLATFORMS}
    platform = platform_map.get(request.platform.strip().casefold())
    if platform is None:
        raise HTTPException(status_code=422, detail="不支持的游戏平台")
    name = request.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="游戏名不能为空")

    existing = None
    if request.game_id:
        existing = GAME_CALENDAR_REGISTRY.get(request.game_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="要修改的游戏不存在")
        game_id = existing.app_id
    else:
        game_id = f"manual-{secrets.token_hex(8)}"

    image_url = existing.image_url if existing else None
    if request.image_data:
        image_url = _save_game_image(game_id, request.image_data)
    game = CalendarGame(game_id, name, request.release_date, image_url, platform)
    _, saved_game = GAME_CALENDAR_REGISTRY.add_resolved(game, "admin")
    _write_admin_action(f"管理台保存游戏发售日历：{name} {request.release_date.isoformat()}")
    return {
        "status": "ok",
        "message": "游戏资料已保存。",
        "item": _game_calendar_payload(saved_game),
    }


def delete_game_calendar_entry(
    game_id: str,
    _: Annotated[None, Depends(require_admin)],
) -> dict[str, object]:
    game = GAME_CALENDAR_REGISTRY.remove(game_id)
    if game is None:
        raise HTTPException(status_code=404, detail="游戏不存在")
    local_image = _local_game_image_path(game.image_url)
    if local_image is not None:
        try:
            local_image.unlink(missing_ok=True)
        except OSError:
            pass
    _write_admin_action(f"管理台删除游戏发售日历：{game.name}")
    return {"status": "ok", "message": "游戏已从发售日历删除。"}


def game_calendar_image(
    game_id: str,
    _: Annotated[None, Depends(require_admin)],
) -> Response:
    game = GAME_CALENDAR_REGISTRY.get(game_id)
    if game is None or not game.image_url:
        raise HTTPException(status_code=404, detail="游戏图片不存在")
    local_image = _local_game_image_path(game.image_url)
    if local_image is not None:
        try:
            return Response(
                content=local_image.read_bytes(),
                media_type="image/jpeg",
                headers={"Cache-Control": "no-store"},
            )
        except OSError as exc:
            raise HTTPException(status_code=404, detail="游戏图片文件不存在") from exc
    return RedirectResponse(game.image_url, status_code=307)


def launch_action(
    action: str,
    _: Annotated[None, Depends(require_admin)],
) -> dict[str, object]:
    scripts = {
        "switch-login": "switch-login.cmd",
        "psn-login": "ps5-login.cmd",
        "xbox-login": "xbox-login.cmd",
        "restart-bot": "restart-qq-bot.cmd",
    }
    script_name = scripts.get(action)
    if not script_name:
        raise HTTPException(status_code=404, detail="未知操作")
    script = PROJECT_ROOT / "scripts" / "windows" / script_name
    if not script.is_file():
        raise HTTPException(status_code=500, detail=f"缺少脚本：{script_name}")
    if action == "restart-bot":
        previous_pid = _heartbeat().get("pid")
        _write_admin_action("管理台请求重启 QQ Bot")
        try:
            with ADMIN_ACTION_LOG_PATH.open("a", encoding="utf-8") as output:
                process = subprocess.Popen(
                    [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", "call", str(script)],
                    cwd=str(PROJECT_ROOT),
                    creationflags=CREATE_NO_WINDOW,
                    close_fds=True,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                )
        except OSError as exc:
            _write_admin_action(f"QQ Bot 重启命令启动失败：{type(exc).__name__}", "ERROR")
            raise HTTPException(status_code=500, detail="无法启动 QQ Bot 重启命令") from exc
        return {
            "status": "ok",
            "message": "重启指令已发送，正在等待新的应用心跳。",
            "previous_pid": previous_pid,
            "command_pid": process.pid,
        }

    subprocess.Popen(
        [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/k", "call", str(script)],
        cwd=str(PROJECT_ROOT),
        creationflags=CREATE_NEW_CONSOLE,
        close_fds=True,
    )
    _write_admin_action(f"管理台请求打开 {script_name}")
    return {"status": "ok", "message": "操作窗口已打开。"}

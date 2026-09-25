from pathlib import Path
import base64
from io import BytesIO

from fastapi.testclient import TestClient
from PIL import Image

from app import admin
from app.main import app
from qq_bot.game_calendar import GameCalendarRegistry


def _client(monkeypatch) -> TestClient:
    monkeypatch.setattr(admin, "_loopback", lambda _request: True)
    client = TestClient(app, client=("127.0.0.1", 50001))
    client.cookies.set("admin_session", admin._issue_admin_session())
    return client


def test_admin_api_requires_local_authenticated_session(monkeypatch) -> None:
    monkeypatch.setattr(admin, "_loopback", lambda _request: True)
    response = TestClient(app).get("/api/admin/status")
    assert response.status_code == 403


def test_admin_page_and_status_never_return_secret_values(monkeypatch) -> None:
    client = _client(monkeypatch)
    page = client.get("/admin")
    assert page.status_code == 200
    assert "游戏状态机器人管理台" in page.text
    payload = client.get("/api/admin/status").json()
    assert set(payload["credentials"].values()) <= {True, False}
    assert "admin-token" not in str(payload)


def test_admin_login_uses_one_time_redirect_and_random_session(monkeypatch) -> None:
    monkeypatch.setattr(admin, "_loopback", lambda _request: True)
    client = TestClient(app, client=("127.0.0.1", 50002), follow_redirects=False)

    response = client.get(f"/admin?token={admin.ADMIN_TOKEN}")

    assert response.status_code == 303
    assert response.headers["location"] == "/admin"
    session = client.cookies.get("admin_session")
    assert session
    assert session != admin.ADMIN_TOKEN
    assert client.get("/admin").status_code == 200


def test_admin_rejects_main_token_as_session_cookie(monkeypatch) -> None:
    monkeypatch.setattr(admin, "_loopback", lambda _request: True)
    client = TestClient(app, client=("127.0.0.1", 50003))
    client.cookies.set("admin_session", admin.ADMIN_TOKEN)

    assert client.get("/api/admin/status").status_code == 403


def test_admin_updates_only_allowlisted_configuration(tmp_path: Path, monkeypatch) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("QQ_ALLOWED_GROUPS=100\nUNTOUCHED=value\n", encoding="utf-8")
    monkeypatch.setattr(admin, "ENV_PATH", env_path)
    client = _client(monkeypatch)

    response = client.post(
        "/api/admin/config",
        json={"values": {"QQ_ALLOWED_GROUPS": "100,200", "STEAM_POLL_INTERVAL": "90"}},
    )
    assert response.status_code == 200
    content = env_path.read_text(encoding="utf-8")
    assert "QQ_ALLOWED_GROUPS=100,200" in content
    assert "STEAM_POLL_INTERVAL=90" in content
    assert "UNTOUCHED=value" in content

    rejected = client.post("/api/admin/config", json={"values": {"PATH": "bad"}})
    assert rejected.status_code == 422


def test_admin_rejects_newline_in_configuration_values(tmp_path: Path, monkeypatch) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("QQ_ALLOWED_GROUPS=100\n", encoding="utf-8")
    monkeypatch.setattr(admin, "ENV_PATH", env_path)

    response = _client(monkeypatch).post(
        "/api/admin/config",
        json={"values": {"NAPCAT_QQ": "123\nINJECTED=yes"}},
    )

    assert response.status_code == 422
    assert "INJECTED" not in env_path.read_text(encoding="utf-8")


def test_admin_accepts_douyin_browser_cookie_source(tmp_path: Path, monkeypatch) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("DOUYIN_COOKIES_FROM_BROWSER=\n", encoding="utf-8")
    monkeypatch.setattr(admin, "ENV_PATH", env_path)

    response = _client(monkeypatch).post(
        "/api/admin/config",
        json={"values": {"DOUYIN_COOKIES_FROM_BROWSER": "edge"}},
    )

    assert response.status_code == 200
    assert "DOUYIN_COOKIES_FROM_BROWSER=edge" in env_path.read_text(encoding="utf-8")


def test_admin_accepts_xiaohongshu_browser_cookie_source(tmp_path: Path, monkeypatch) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("XIAOHONGSHU_COOKIES_FROM_BROWSER=\n", encoding="utf-8")
    monkeypatch.setattr(admin, "ENV_PATH", env_path)

    response = _client(monkeypatch).post(
        "/api/admin/config",
        json={"values": {"XIAOHONGSHU_COOKIES_FROM_BROWSER": "edge"}},
    )

    assert response.status_code == 200
    assert "XIAOHONGSHU_COOKIES_FROM_BROWSER=edge" in env_path.read_text(encoding="utf-8")


def test_admin_accepts_and_validates_release_reminder_settings(tmp_path: Path, monkeypatch) -> None:
    env_path = tmp_path / ".env"
    monkeypatch.setattr(admin, "ENV_PATH", env_path)
    client = _client(monkeypatch)

    response = client.post(
        "/api/admin/config",
        json={
            "values": {
                "GAME_RELEASE_REMINDER_HOUR": "0",
                "GAME_RELEASE_REMINDER_DAYS": "7,1,0",
                "GAME_RELEASE_REMINDER_INTERVAL": "300",
            }
        },
    )

    assert response.status_code == 200
    content = env_path.read_text(encoding="utf-8")
    assert "GAME_RELEASE_REMINDER_HOUR=0" in content
    assert "GAME_RELEASE_REMINDER_DAYS=7,1,0" in content
    assert client.post(
        "/api/admin/config",
        json={"values": {"GAME_RELEASE_REMINDER_HOUR": "24"}},
    ).status_code == 422
    assert client.post(
        "/api/admin/config",
        json={"values": {"GAME_RELEASE_REMINDER_DAYS": "tomorrow"}},
    ).status_code == 422


def test_admin_saves_secret_without_returning_it(tmp_path: Path, monkeypatch) -> None:
    secret_path = tmp_path / "steam.txt"
    monkeypatch.setattr(
        admin,
        "SECRET_FILES",
        {"steam": ("STEAM_API_KEY_FILE", str(secret_path))},
    )
    monkeypatch.setattr(admin, "ENV_PATH", tmp_path / ".env")
    client = _client(monkeypatch)
    value = "a" * 32

    response = client.post("/api/admin/secrets/steam", json={"value": value})

    assert response.status_code == 200
    assert value not in response.text
    assert secret_path.read_text(encoding="utf-8").strip() == value


def test_admin_saves_openxbl_key_without_returning_it(tmp_path: Path, monkeypatch) -> None:
    secret_path = tmp_path / "openxbl.txt"
    monkeypatch.setattr(
        admin,
        "SECRET_FILES",
        {"openxbl": ("OPENXBL_API_KEY_FILE", str(secret_path))},
    )
    monkeypatch.setattr(admin, "ENV_PATH", tmp_path / ".env")
    value = "openxbl-secret-key-value"

    response = _client(monkeypatch).post(
        "/api/admin/secrets/openxbl", json={"value": value}
    )

    assert response.status_code == 200
    assert value not in response.text
    assert secret_path.read_text(encoding="utf-8").strip() == value


def test_admin_logs_return_console_events_but_filter_chat_noise(tmp_path: Path, monkeypatch) -> None:
    bot_log = tmp_path / "qq-bot.log"
    bot_log.write_text(
        "2026-08-30 10:00:00.000 | DEBUG | nonebot:init:1 - Loaded Config: hidden\n"
        "2026-08-30 10:00:30.000 | SUCCESS | nonebot:event:1 - OneBot V11 | [message.group.normal]: ordinary chat\n"
        "2026-08-30 10:00:40.000 | INFO | nonebot:matcher:1 - Event will be handled by Matcher(type='message')\n"
        "2026-08-30 10:01:00.000 | INFO | qq_bot.steam_commands:x:1 - Steam list sent to group 1\n"
        "2026-08-30 10:02:00.000 | INFO | qq_bot.steam_registry:x:1 - Sent Steam activity for 2 to group 1\n"
        "2026-08-30 10:03:00.000 | WARNING | qq_bot.ps5_registry:x:1 - PSN operation failed\n"
        "Traceback: npsso=do-not-return-this\n",
        encoding="utf-8",
    )
    (tmp_path / "qq-bot.2026-08-30_09-00-00.log").write_text(
        "2026-08-30 09:59:00.000 | ERROR | qq_bot.switch_registry:x:1 - Switch poll failed before rotation\n",
        encoding="utf-8",
    )
    watchdog_log = tmp_path / "watchdog.log"
    watchdog_log.write_text(
        "2026-08-30 10:04:00,000 INFO OneBot application heartbeat recovered and remained stable\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(admin, "BOT_LOG_PATH", bot_log)
    monkeypatch.setattr(admin, "WATCHDOG_LOG_PATH", watchdog_log)
    monkeypatch.setattr(admin, "ADMIN_ACTION_LOG_PATH", tmp_path / "actions.log")

    payload = _client(monkeypatch).get("/api/admin/logs").json()
    messages = "\n".join(item["message"] for item in payload["entries"])

    assert "Sent Steam activity" in messages
    assert "PSN operation failed" in messages
    assert "heartbeat recovered" in messages
    assert "Switch poll failed before rotation" in messages
    assert "Loaded Config" not in messages
    assert "ordinary chat" not in messages
    assert "Event will be handled" not in messages
    assert "Steam list sent" in messages
    assert "do-not-return-this" not in messages


def test_admin_status_exposes_bot_runtime_components(monkeypatch) -> None:
    monkeypatch.setattr(admin, "_heartbeat", lambda: {
        "fresh": True,
        "connected": True,
        "age_seconds": 1.0,
        "pid": 1234,
        "uptime": "2小时15分钟",
        "components": {
            "qq": {"state": "ok", "detail": "已连接", "failures": 0},
            "steam": {"state": "error", "detail": "服务超时", "failures": 2},
        },
    })

    payload = _client(monkeypatch).get("/api/admin/status").json()

    assert payload["bot_status"]["components"]["qq"]["detail"] == "已连接"
    assert payload["bot_status"]["components"]["steam"]["state"] == "error"
    assert payload["bot_status"]["components"]["switch"]["state"] == "starting"
    assert payload["bot_status"]["uptime"] == "2小时15分钟"


def test_restart_action_runs_hidden_and_returns_previous_heartbeat(
    tmp_path: Path, monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    class Process:
        pid = 4321

    def fake_popen(command, **options):
        captured["command"] = command
        captured.update(options)
        return Process()

    monkeypatch.setattr(admin, "ADMIN_ACTION_LOG_PATH", tmp_path / "actions.log")
    monkeypatch.setattr(admin, "_heartbeat", lambda: {
        "fresh": True, "connected": True, "age_seconds": 1.0, "pid": 1234,
    })
    monkeypatch.setattr(admin.subprocess, "Popen", fake_popen)

    response = _client(monkeypatch).post("/api/admin/actions/restart-bot")

    assert response.status_code == 200
    assert response.json()["previous_pid"] == 1234
    assert response.json()["command_pid"] == 4321
    assert captured["creationflags"] == admin.CREATE_NO_WINDOW
    command = " ".join(str(part) for part in captured["command"])
    assert "scripts" in command
    assert "windows" in command
    assert "restart-qq-bot.cmd" in command


def test_admin_can_create_edit_preview_and_delete_calendar_game(
    tmp_path: Path, monkeypatch,
) -> None:
    registry = GameCalendarRegistry(tmp_path / "calendar.db")
    image_dir = tmp_path / "images"
    monkeypatch.setattr(admin, "GAME_CALENDAR_REGISTRY", registry)
    monkeypatch.setattr(admin, "LOCAL_IMAGE_DIR", image_dir)
    output = BytesIO()
    Image.new("RGB", (320, 180), "#e60012").save(output, format="PNG")
    image_data = "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii")
    client = _client(monkeypatch)

    created = client.post(
        "/api/admin/game-calendar",
        json={
            "name": "Switch 独占游戏",
            "platform": "Switch",
            "release_date": "2026-09-18",
            "image_data": image_data,
        },
    )
    assert created.status_code == 200
    game_id = created.json()["item"]["game_id"]
    assert created.json()["item"]["has_image"] is True

    listing = client.get("/api/admin/game-calendar").json()["items"]
    assert listing[0]["name"] == "Switch 独占游戏"
    assert listing[0]["platform"] == "Switch"
    preview = client.get(f"/api/admin/game-calendar/{game_id}/image")
    assert preview.status_code == 200
    assert preview.headers["content-type"] == "image/jpeg"

    edited = client.post(
        "/api/admin/game-calendar",
        json={
            "game_id": game_id,
            "name": "修改后的游戏名",
            "platform": "PS",
            "release_date": "2026-09-20",
            "image_data": None,
        },
    )
    assert edited.status_code == 200
    assert edited.json()["item"]["name"] == "修改后的游戏名"
    assert edited.json()["item"]["has_image"] is True

    deleted = client.delete(f"/api/admin/game-calendar/{game_id}")
    assert deleted.status_code == 200
    assert client.get("/api/admin/game-calendar").json()["items"] == []
    assert list(image_dir.glob("*")) == []

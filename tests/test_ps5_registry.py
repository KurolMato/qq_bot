import asyncio
import sqlite3
from pathlib import Path

import pytest

from qq_bot.ps5_registry import (
    Ps5Client,
    Ps5PresenceMonitor,
    Ps5Registry,
    Ps5Subscription,
    Ps5VisibilityError,
    _avatar_url,
    _catalog_cover_url,
    _should_notify,
    normalize_online_id,
    parse_ps5_activity,
    psn_user_message,
)


def subscription(**overrides: object) -> Ps5Subscription:
    values: dict[str, object] = {
        "online_id": "Player-One",
        "account_id": "1234567890",
        "avatar_url": "https://example.com/avatar.png",
        "qq_user_id": "100",
        "group_id": "200",
        "status": "active",
        "current_game": None,
        "initialised": False,
        "force_notify": False,
    }
    values.update(overrides)
    return Ps5Subscription(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", ["Player-One", "abc", "A_12345678901234"])
def test_normalize_online_id(value: str) -> None:
    assert normalize_online_id(value) == value


@pytest.mark.parametrize("value", ["12", "_player", "player name", "A" * 17])
def test_reject_invalid_online_id(value: str) -> None:
    with pytest.raises(ValueError):
        normalize_online_id(value)


def test_registry_supports_multiple_groups_and_group_member_remove(tmp_path: Path) -> None:
    registry = Ps5Registry(tmp_path / "ps5.db")
    first = subscription()
    second = subscription(group_id="201", qq_user_id="101")
    registry.add(first)
    registry.add(second)

    assert registry.list_group("200") == [first]
    assert registry.list_group("201") == [second]
    assert registry.remove("player-one", "200", "other") is True
    assert registry.list_group("201") == [second]


def test_parse_ps5_activity() -> None:
    activity = parse_ps5_activity(
        subscription(),
        {
            "accountId": "1234567890",
            "primaryPlatformInfo": {"onlineStatus": "online", "platform": "PS5"},
            "gameTitleInfoList": [
                {
                    "titleName": "Astro Bot",
                    "npTitleIconUrl": "https://example.com/astro.png",
                    "launchPlatform": "PS5",
                }
            ],
        },
    )

    assert activity.account_name == "Player-One"
    assert activity.game_name == "Astro Bot"
    assert activity.game_image_url == "https://example.com/astro.png"
    assert activity.game_key == "astro bot"
    assert activity.platform == "PS"


def test_catalog_cover_prefers_portrait_art() -> None:
    details = [{
        "media": {
            "images": [
                {"type": "GAMEHUB_COVER_ART", "url": "https://example.com/wide.png"},
                {"type": "PORTRAIT_BANNER", "url": "https://example.com/portrait.png"},
            ]
        }
    }]

    assert _catalog_cover_url(details) == "https://example.com/portrait.png"


def test_default_psn_avatar_is_upgraded_to_https() -> None:
    assert _avatar_url({
        "avatars": [{
            "size": "xl",
            "url": (
                "http://static-resource.np.community.playstation.net/"
                "avatar_xl/default/Defaultavatar_xl.png"
            ),
        }]
    }) == (
        "https://static-resource.np.community.playstation.net/"
        "avatar_xl/default/Defaultavatar_xl.png"
    )


def test_registry_migrates_existing_default_avatar_to_https(tmp_path: Path) -> None:
    path = tmp_path / "ps5.db"
    registry = Ps5Registry(path)
    registry.add(subscription())
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE ps5_subscriptions SET avatar_url = ?",
            (
                "http://static-resource.np.community.playstation.net/"
                "avatar_xl/default/Defaultavatar_xl.png",
            ),
        )

    migrated = Ps5Registry(path).list_group("200")[0]
    assert migrated.avatar_url == (
        "https://static-resource.np.community.playstation.net/"
        "avatar_xl/default/Defaultavatar_xl.png"
    )


def test_ps5_notification_only_on_start_or_change() -> None:
    assert _should_notify(subscription(), "astro bot") is True
    assert _should_notify(subscription(initialised=True, current_game="Astro Bot"), "astro bot") is False
    assert _should_notify(subscription(initialised=True, current_game="Astro Bot"), "helldivers 2") is True
    assert _should_notify(subscription(initialised=True, current_game="Astro Bot"), None) is False


@pytest.mark.parametrize(("error", "expected"), [
    (RuntimeError("404 Not Found"), "没有找到这个 PSN 在线 ID"),
    (RuntimeError("403 Forbidden"), "请把“在线状态和当前游戏”设为所有人可见"),
    (RuntimeError("401 Unauthorized"), "PSN 观察账号登录已失效"),
    (RuntimeError("C:/Users/name/private stack"), "PSN 服务暂时无法完成操作"),
])
def test_psn_errors_are_safe_for_group(error: BaseException, expected: str) -> None:
    message = psn_user_message(error)
    assert expected in message
    assert "C:/" not in message
    assert "stack" not in message


def test_forbidden_presence_uses_visibility_error() -> None:
    client = Ps5Client()

    def forbidden() -> None:
        raise RuntimeError("403 Forbidden")

    with pytest.raises(Ps5VisibilityError, match="在线状态和当前游戏"):
        asyncio.run(client._call("presence", forbidden))


def test_avatar_refresh_deduplicates_and_preserves_presence(tmp_path: Path) -> None:
    from unittest.mock import AsyncMock
    from dataclasses import replace

    registry = Ps5Registry(tmp_path / "ps5.db")
    first = subscription(initialised=True, current_game="Astro Bot", nickname="Friend")
    registry.add(first)
    registry.add(replace(first, group_id="201"))
    registry.add(subscription(account_id="failed"))
    registry.add(subscription(account_id="empty"))
    client = Ps5Client()
    client.avatar = AsyncMock(side_effect=["https://example.com/new.png", RuntimeError(), None])
    monitor = Ps5PresenceMonitor(registry, client)
    assert asyncio.run(monitor.refresh_avatars()) == {"checked": 3, "updated": 1, "failed": 2}
    assert client.avatar.await_count == 3
    assert registry.list_group("201") == [replace(first, group_id="201", avatar_url="https://example.com/new.png")]
    # A presence poll holding an older subscription must not undo the refresh.
    registry.update_presence(first, parse_ps5_activity(first, {}), status="active", initialised=True)
    rows = registry.list_group("200")
    assert rows[0].avatar_url == "https://example.com/new.png"
    assert rows[0].nickname == "Friend"
    assert rows[1].avatar_url == rows[2].avatar_url == first.avatar_url


def test_avatar_refresh_once_per_local_date(tmp_path: Path, monkeypatch) -> None:
    from datetime import datetime
    from unittest.mock import AsyncMock
    import qq_bot.ps5_registry as module

    class Clock(datetime):
        current = datetime(2026, 9, 17, 23, 59)

        @classmethod
        def now(cls):
            return cls.current

    monkeypatch.setattr(module, "datetime", Clock)
    monitor = Ps5PresenceMonitor(Ps5Registry(tmp_path / "ps5.db"), Ps5Client())
    monitor.refresh_avatars = AsyncMock()

    async def run():
        await monitor.refresh_avatars_if_due()
        await monitor.refresh_avatars_if_due()
        assert monitor.refresh_avatars.await_count == 1
        Clock.current = datetime(2026, 9, 18)
        await monitor.refresh_avatars_if_due()
        assert monitor.refresh_avatars.await_count == 2

    asyncio.run(run())


def test_monitor_checks_presence_before_avatar_refresh(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    import qq_bot.ps5_registry as module

    monitor = Ps5PresenceMonitor(Ps5Registry(tmp_path / "ps5.db"), SimpleNamespace(configured=True))
    events = []
    monitor.poll_once = AsyncMock(side_effect=lambda: events.append("poll"))
    monkeypatch.setattr(module, "mark_success", lambda *args: events.append("healthy"))

    async def avatars():
        events.append("avatars")
        await monitor.stop()

    monitor.refresh_avatars_if_due = avatars
    asyncio.run(monitor.run())
    assert events == ["poll", "healthy", "avatars"]


def test_monitor_reports_poll_timeout(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    import qq_bot.ps5_registry as module

    monitor = Ps5PresenceMonitor(Ps5Registry(tmp_path / "ps5.db"), SimpleNamespace(configured=True))
    monitor.poll_once = AsyncMock(side_effect=TimeoutError)
    failures = []
    monkeypatch.setattr(module, "mark_failure", lambda *args: failures.append(args))

    async def stop(*args):
        await monitor.stop()

    monkeypatch.setattr(module, "wait_for_stop", stop)
    asyncio.run(monitor.run())
    assert failures == [("ps5", "PSN 请求超时，正在自动重试")]


def test_avatar_timeout_keeps_daily_refresh_bounded(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    import qq_bot.ps5_registry as module

    monitor = Ps5PresenceMonitor(Ps5Registry(tmp_path / "ps5.db"), Ps5Client())
    monitor.refresh_avatars = AsyncMock(side_effect=TimeoutError)

    async def run():
        await monitor.refresh_avatars_if_due()
        await monitor.refresh_avatars_if_due()

    asyncio.run(run())
    assert monitor.refresh_avatars.await_count == 1

from pathlib import Path

import pytest

from qq_bot.switch_registry import (
    SwitchRegistry,
    SwitchSubscription,
    _is_transient_nxapi_error,
    _nxapi_login_pause_seconds,
    _should_notify,
    nxapi_user_message,
    normalize_friend_code,
)


@pytest.mark.parametrize("raw", [
    "SW-1234-5678-9012",
    "1234-5678-9012",
    "sw 1234 5678 9012",
])
def test_normalize_friend_code(raw: str) -> None:
    assert normalize_friend_code(raw) == "1234-5678-9012"


def test_rejects_invalid_friend_code() -> None:
    with pytest.raises(ValueError):
        normalize_friend_code("1234-5678")


@pytest.mark.parametrize(("detail", "expected"), [
    ("9427 Upgrade required at loginWithNintendoAccountToken", "更新客户端版本"),
    ("Too many attempts to authenticate (coral)", "登录尝试过于频繁"),
    ("Remote configuration prevents Coral authentication", "检查 nxapi 兼容更新或上游状态"),
    ("status: 9402, errorMessage: Resource not found.", "没有找到这个 Switch 好友码"),
    ("CoralStatus.RATE_LIMIT_EXCEEDED 9437", "操作过于频繁"),
    ("CoralStatus.RECEIVER_FRIEND_LIMIT_EXCEEDED 9462", "对方的 Switch 好友数量已达上限"),
    ("EPERM: operation not permitted, shared credential file is locked", "正在处理其他请求"),
    ("Error: C:/Users/example/private/path stack trace", "Switch 服务暂时无法完成操作"),
])
def test_nxapi_error_is_safe_for_group(detail: str, expected: str) -> None:
    message = nxapi_user_message(detail)
    assert expected in message
    assert "C:/" not in message
    assert "stack" not in message


@pytest.mark.parametrize("detail", [
    "TypeError: fetch failed; cause ECONNRESET",
    "Client network socket disconnected before secure TLS connection was established",
    "502 Bad Gateway; retry-after: 60",
])
def test_transient_nxapi_errors_are_retryable(detail: str) -> None:
    assert _is_transient_nxapi_error(detail) is True
    assert "网络连接暂时不稳定" in nxapi_user_message(detail)


@pytest.mark.parametrize(("detail", "seconds"), [
    ("status: 9427, errorMessage: Upgrade required.", 1800.0),
    ("Remote configuration prevents Coral authentication", 1800.0),
    ("Too many attempts to authenticate (coral)", 3600.0),
])
def test_login_failures_pause_automatic_authentication(detail: str, seconds: float) -> None:
    assert _nxapi_login_pause_seconds(detail) == seconds


def test_regular_nxapi_failure_does_not_trigger_login_pause() -> None:
    assert _nxapi_login_pause_seconds("502 Bad Gateway") is None


def test_registry_add_list_and_group_member_remove(tmp_path: Path) -> None:
    registry = SwitchRegistry(tmp_path / "registry.db")
    item = SwitchSubscription(
        friend_code="1234-5678-9012", nsa_id="0123456789abcdef", ns_name="小明",
        avatar_url=None, qq_user_id="100", group_id="200", status="pending",
        current_game=None, initialised=False,
    )
    registry.add(item)
    assert registry.list_group("200") == [item]
    assert registry.remove(item.friend_code, "200", "other") is True
    assert registry.list_group("200") == []


def test_same_friend_can_be_registered_in_two_groups(tmp_path: Path) -> None:
    registry = SwitchRegistry(tmp_path / "registry.db")
    first = SwitchSubscription(
        friend_code="1234-5678-9012", nsa_id="0123456789abcdef", ns_name="小明",
        avatar_url=None, qq_user_id="100", group_id="200", status="active",
        current_game=None, initialised=False,
    )
    second = SwitchSubscription(
        friend_code=first.friend_code, nsa_id=first.nsa_id, ns_name=first.ns_name,
        avatar_url=None, qq_user_id="101", group_id="201", status="active",
        current_game=None, initialised=False,
    )

    registry.add(first)
    registry.add(second)

    assert registry.list_group("200") == [first]
    assert registry.list_group("201") == [second]


def test_new_group_subscription_notifies_current_game() -> None:
    item = SwitchSubscription(
        friend_code="1234-5678-9012", nsa_id="0123456789abcdef", ns_name="小明",
        avatar_url=None, qq_user_id="100", group_id="200", status="active",
        current_game=None, initialised=False,
    )

    assert _should_notify(item, "splatoon 3") is True
    assert _should_notify(item, None) is False


def test_forced_subscription_retries_even_with_same_game() -> None:
    item = SwitchSubscription(
        friend_code="1234-5678-9012", nsa_id="0123456789abcdef", ns_name="小明",
        avatar_url=None, qq_user_id="100", group_id="200", status="active",
        current_game="Splatoon 3", initialised=True, force_notify=True,
    )

    assert _should_notify(item, "splatoon 3") is True

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import nonebot
import pytest
from nonebot.adapters.onebot.v11 import Message
from nonebot.adapters.onebot.v11.exception import ActionFailed, NetworkError


@pytest.mark.parametrize("argument", ["", "me", "invalid"])
@pytest.mark.parametrize("timeout", [False, "network", "napcat"])
def test_rank_delivery_does_not_retry(monkeypatch, caplog, argument, timeout):
    nonebot.init()
    from qq_bot import game_time_commands as commands

    snapshot = SimpleNamespace(members=(object(),))
    monkeypatch.setattr(commands, "config", SimpleNamespace(allowed_groups=set()))
    monkeypatch.setattr(commands, "tracker", SimpleNamespace(
        snapshot=lambda *a, **kw: snapshot,
        bound_member=lambda *a: "Tester",
        personal_snapshot=lambda *a, **kw: snapshot,
    ))
    rendered = Message("rendered leaderboard")
    monkeypatch.setattr(commands, "build_leaderboard_message", AsyncMock(return_value=rendered))
    monkeypatch.setattr(commands, "build_personal_rank_message", AsyncMock(return_value=rendered))
    error = None
    if timeout == "network":
        error = NetworkError("WebSocket call api send_msg timeout")
    elif timeout == "napcat":
        error = ActionFailed(status="failed", retcode=1200, message=(
            "Timeout: NTEvent serviceAndMethod:NodeIKernelMsgService/sendMsg "
            "ListenerName:NodeIKernelMsgListener/onMsgInfoListUpdate EventRet:\n{}\n"
        ))
    send = AsyncMock(side_effect=error)
    event = SimpleNamespace(group_id=100, user_id=200)
    asyncio.run(commands.handle_rank_command(SimpleNamespace(send=send), event, Message(argument)))
    send.assert_awaited_once()
    if argument != "invalid":
        assert send.await_args.args == (event, rendered)
    assert ("delivery unconfirmed for group 100" in caplog.text) == bool(timeout)


def test_unexpected_send_error_is_not_hidden():
    nonebot.init()
    from qq_bot.game_time_commands import _send_reply

    bot = SimpleNamespace(send=AsyncMock(side_effect=RuntimeError("unexpected")))
    with pytest.raises(RuntimeError, match="unexpected"):
        asyncio.run(_send_reply(bot, SimpleNamespace(group_id=100), "reply"))


@pytest.mark.parametrize("argument,is_dashboard", [
    ("me", True), ("我", True), ("me d", False), ("me y", False), ("me w", False),
    ("me m", False), ("player Tester", False), ("player m Tester", False),
])
def test_personal_dashboard_routing(monkeypatch, argument, is_dashboard):
    nonebot.init()
    from qq_bot import game_time_commands as commands

    snapshot = object()
    tracker = SimpleNamespace(
        bound_member=lambda *args: "Tester",
        personal_snapshot=lambda *args, **kwargs: snapshot,
    )
    dashboard = AsyncMock(return_value=Message("dashboard"))
    period_card = AsyncMock(return_value=Message("period"))
    monkeypatch.setattr(commands, "config", SimpleNamespace(allowed_groups=set()))
    monkeypatch.setattr(commands, "tracker", tracker)
    monkeypatch.setattr(commands, "build_personal_rank_message", dashboard)
    monkeypatch.setattr(commands, "build_leaderboard_message", period_card)
    bot = SimpleNamespace(send=AsyncMock())
    event = SimpleNamespace(group_id=100, user_id=200)
    asyncio.run(commands.handle_rank_command(bot, event, Message(argument)))
    assert dashboard.await_count == int(is_dashboard)
    assert period_card.await_count == int(not is_dashboard)
    bot.send.assert_awaited_once()
    if is_dashboard:
        dashboard.assert_awaited_once_with(tracker, "100", "Tester")


def test_unbound_personal_dashboard_does_not_start_loading(monkeypatch):
    nonebot.init()
    from qq_bot import game_time_commands as commands

    monkeypatch.setattr(commands, "config", SimpleNamespace(allowed_groups=set()))
    monkeypatch.setattr(commands, "tracker", SimpleNamespace(bound_member=lambda *args: None))
    dashboard = AsyncMock()
    monkeypatch.setattr(commands, "build_personal_rank_message", dashboard)
    bot = SimpleNamespace(send=AsyncMock())
    event = SimpleNamespace(group_id=100, user_id=200)
    asyncio.run(commands.handle_rank_command(bot, event, Message("me")))
    dashboard.assert_not_awaited()
    assert "绑定" in bot.send.await_args.args[1]


@pytest.mark.parametrize("argument,month", [("me 9", 9), ("me 09", 9), ("me 12", 12)])
def test_personal_month_routes_to_final_dashboard(monkeypatch, argument, month):
    nonebot.init()
    from qq_bot import game_time_commands as commands

    source = SimpleNamespace(bound_member=lambda *args: "Tester")
    dashboard = AsyncMock(return_value=Message("final image"))
    monkeypatch.setattr(commands, "config", SimpleNamespace(allowed_groups=set()))
    monkeypatch.setattr(commands, "tracker", source)
    monkeypatch.setattr(commands, "build_personal_rank_message", dashboard)
    bot = SimpleNamespace(send=AsyncMock())
    event = SimpleNamespace(group_id=100, user_id=200)
    asyncio.run(commands.handle_rank_command(bot, event, Message(argument)))
    dashboard.assert_awaited_once_with(source, "100", "Tester", month=month)
    bot.send.assert_awaited_once_with(event, dashboard.return_value)
    assert not commands._pending_personal_profiles


@pytest.mark.parametrize("argument", ["me 0", "me 13", "me -1", "me 9.5", "me 9 extra", "me m 9"])
def test_invalid_personal_month_replies_with_usage(monkeypatch, argument):
    nonebot.init()
    from qq_bot import game_time_commands as commands

    dashboard = AsyncMock()
    monkeypatch.setattr(commands, "config", SimpleNamespace(allowed_groups=set()))
    monkeypatch.setattr(commands, "tracker", SimpleNamespace())
    monkeypatch.setattr(commands, "build_personal_rank_message", dashboard)
    bot = SimpleNamespace(send=AsyncMock())
    asyncio.run(commands.handle_rank_command(bot, SimpleNamespace(group_id=100, user_id=200), Message(argument)))
    dashboard.assert_not_awaited()
    assert "/rank me 9" in bot.send.await_args.args[1]
    assert not commands._pending_personal_profiles


@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("argument", ["me", "me 9"])
def test_pending_dashboard_suppresses_duplicates_and_cleans_up(monkeypatch, cancel, argument):
    nonebot.init()
    from qq_bot import game_time_commands as commands

    monkeypatch.setattr(commands, "config", SimpleNamespace(allowed_groups=set()))
    monkeypatch.setattr(commands, "tracker", SimpleNamespace(bound_member=lambda *args: "Tester"))
    monkeypatch.setattr(commands, "_pending_personal_profiles", set())
    bot = SimpleNamespace(send=AsyncMock())
    event = SimpleNamespace(group_id=100, user_id=200)

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def build(*args, **kwargs):
            entered.set()
            await release.wait()
            return Message("ready image")

        dashboard = AsyncMock(side_effect=build)
        monkeypatch.setattr(commands, "build_personal_rank_message", dashboard)
        pending = asyncio.create_task(commands.handle_rank_command(bot, event, Message(argument)))
        await asyncio.wait_for(entered.wait(), timeout=2)
        await commands.handle_rank_command(bot, event, Message(argument))
        bot.send.assert_not_awaited()
        dashboard.assert_awaited_once()
        if cancel:
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            bot.send.assert_not_awaited()
        else:
            release.set()
            await pending
            bot.send.assert_awaited_once()
        assert not commands._pending_personal_profiles

    asyncio.run(scenario())

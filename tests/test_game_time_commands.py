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

from pathlib import Path

import pytest

from qq_bot.nicknames import list_line, normalize_nickname
from qq_bot.ps5_registry import Ps5Registry, Ps5Subscription
from qq_bot.steam_registry import SteamRegistry, SteamSubscription
from qq_bot.switch_registry import SwitchRegistry, SwitchSubscription


def test_nickname_validation_and_list_line() -> None:
    assert normalize_nickname("  小 明  ") == "小 明"
    assert list_line("小明", "Splatoon 3") == "小明｜视奸中｜Splatoon 3"
    assert list_line("小明", None) == "小明｜视奸中｜未在游戏"
    with pytest.raises(ValueError):
        normalize_nickname("x" * 21)


def test_same_nickname_can_bind_all_platforms(tmp_path: Path) -> None:
    switch = SwitchRegistry(tmp_path / "switch.db")
    psn = Ps5Registry(tmp_path / "psn.db")
    steam = SteamRegistry(tmp_path / "steam.db")
    nickname = "群友小明"

    switch.add(SwitchSubscription(
        friend_code="1234-5678-9012", nsa_id="nsa", ns_name="NS name",
        avatar_url=None, qq_user_id="100", group_id="200", status="active",
        current_game=None, initialised=False,
    ))
    psn.add(Ps5Subscription(
        online_id="Player-One", account_id="account", avatar_url=None,
        qq_user_id="100", group_id="200", status="active", current_game=None,
        initialised=False,
    ))
    steam.add(SteamSubscription(
        steam_id="76561198000000000", display_name="Steam name",
        identifier="steam_name", avatar_url=None, qq_user_id="100", group_id="200",
        status="active", current_game=None, initialised=False,
    ))

    assert switch.set_nickname("1234-5678-9012", "200", "other", nickname) is True
    assert psn.set_nickname("player-one", "200", "other", nickname) is True
    assert steam.set_nickname("steam_name", "200", "other", nickname) is True
    assert switch.list_group("200")[0].nickname == nickname
    assert psn.list_group("200")[0].nickname == nickname
    assert steam.list_group("200")[0].nickname == nickname

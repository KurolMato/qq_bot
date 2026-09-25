from io import BytesIO
from pathlib import Path

from PIL import Image
import pytest

from qq_bot import list_cards as cards
from qq_bot.list_cards import ListCardEntry, render_list_card, render_online_overview


def test_load_assets_with_no_downloadable_images():
    import asyncio
    from qq_bot.list_cards import _load_assets

    assert asyncio.run(_load_assets([])) == {}
    assert asyncio.run(_load_assets([ListCardEntry("Player", "Game")])) == {}


def _asset(colour: str) -> bytes:
    image = Image.new("RGB", (160, 160), colour)
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def test_list_card_contains_every_member_in_one_image() -> None:
    entries = [
        ListCardEntry(
            name=f"玩家 {index}",
            current_game=f"游戏 {index}" if index % 2 else None,
            avatar_url=f"avatar-{index}",
            game_image_url=f"game-{index}" if index % 2 else None,
        )
        for index in range(12)
    ]
    assets = {
        **{f"avatar-{index}": _asset("#426b8a") for index in range(12)},
        **{f"game-{index}": _asset("#ce5b4c") for index in range(1, 12, 2)},
    }

    content = render_list_card("switch", entries, assets)

    with Image.open(BytesIO(content)) as result:
        assert result.format == "PNG"
        assert result.width == 1000
        assert result.height == 36 + 12 * 146 + 11 * 12


def test_list_card_supports_each_platform_style() -> None:
    entry = ListCardEntry("玩家", "游戏")

    for platform in ("switch", "ps", "steam", "xbox"):
        content = render_list_card(platform, [entry])
        with Image.open(BytesIO(content)) as result:
            assert result.size == (1000, 182)


def test_online_overview_uses_four_fixed_columns_and_current_players() -> None:
    groups = {
        "steam": [ListCardEntry("Steam 玩家", "游戏 A", "avatar-a", "game-a")],
        "ps": [ListCardEntry("PS 玩家", "游戏 B")],
        "switch": [ListCardEntry("Switch 玩家", "游戏 C")],
        "xbox": [ListCardEntry("Xbox 玩家", "游戏 D")],
    }
    content = render_online_overview(
        groups,
        {"avatar-a": _asset("#416b91"), "game-a": _asset("#cc5544")},
    )

    with Image.open(BytesIO(content)) as result:
        assert result.format == "PNG"
        assert result.width == 1560
        assert result.height == 350


def test_online_overview_keeps_empty_platform_column() -> None:
    content = render_online_overview(
        {"steam": [ListCardEntry("玩家", "游戏")], "ps": [], "switch": [], "xbox": []}
    )
    with Image.open(BytesIO(content)) as result:
        assert result.size == (1560, 350)


def test_online_overview_replaces_unhealthy_platform_rows() -> None:
    content = render_online_overview(
        {
            "steam": [ListCardEntry("过期玩家", "过期游戏", "avatar", "cover")],
            "ps": [ListCardEntry("正常玩家", "正常游戏")],
            "switch": [],
            "xbox": [],
        },
        {"avatar": _asset("#416b91"), "cover": _asset("#cc5544")},
        {"steam"},
    )
    with Image.open(BytesIO(content)) as result:
        assert result.size == (1560, 350)


def test_image_cache_pruning_keeps_a_bounded_number_of_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cards, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(cards, "CACHE_MAX_FILES", 3)
    monkeypatch.setattr(cards, "CACHE_MAX_AGE_SECONDS", 86400.0)
    files = []
    for index in range(5):
        path = tmp_path / f"{index}.img"
        path.write_bytes(_asset("#426b8a"))
        files.append(path)

    cards._prune_cache(files[-1])

    remaining = list(tmp_path.iterdir())
    assert len(remaining) == 3
    assert files[-1] in remaining

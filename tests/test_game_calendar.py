import asyncio
from datetime import date
from io import BytesIO
from pathlib import Path

from PIL import Image

from qq_bot.game_calendar import (
    CalendarGame,
    GameCalendarRegistry,
    SteamGameMatcher,
    _calendar_cover,
    _render_calendar,
    _render_year_calendar,
    build_year_calendar_message,
    calendar_year_months,
    build_calendar_message,
    build_release_reminder_message,
    game_names_highly_similar,
    parse_month_selector,
    parse_release_date,
    parse_store_release_timestamp,
)


def test_parse_steam_release_dates() -> None:
    assert parse_release_date("2026 年 9 月 18 日") == date(2026, 9, 18)
    assert parse_release_date("Sep 18, 2026") == date(2026, 9, 18)
    assert parse_release_date("即将推出") is None


def test_store_release_timestamp_uses_china_calendar_date() -> None:
    html = 'app_release_date&quot;:&quot;1789606800&quot;'
    assert parse_store_release_timestamp(html) == date(2026, 9, 17)
    assert parse_store_release_timestamp("missing") is None


def test_calendar_registry_is_shared_and_sorted(tmp_path: Path) -> None:
    registry = GameCalendarRegistry(tmp_path / "calendar.db")
    later = CalendarGame("2", "Later", date(2026, 9, 20), "https://example.com/2.jpg")
    earlier = CalendarGame("1", "Earlier", date(2026, 9, 3), "https://example.com/1.jpg")

    assert registry.add(later, "100") is True
    assert registry.add(earlier, "200") is True
    assert registry.add(earlier, "300") is False

    assert registry.list_month(2026, 9) == [earlier, later]
    assert registry.list_month(2026, 10) == []


def test_calendar_registry_lists_date_and_persists_reminder_receipt(tmp_path: Path) -> None:
    registry = GameCalendarRegistry(tmp_path / "calendar.db")
    target = date(2026, 9, 17)
    game = CalendarGame("1", "Example", target, None)
    registry.add(game, "100")

    assert registry.list_date(target) == [game]
    assert registry.reminder_was_sent("200", target, 1) is False
    registry.mark_reminder_sent("200", target, 1)
    assert registry.reminder_was_sent("200", target, 1) is True
    assert registry.reminder_was_sent("201", target, 1) is False


def test_cross_platform_duplicate_keeps_steam_version(tmp_path: Path) -> None:
    registry = GameCalendarRegistry(tmp_path / "calendar.db")
    ps_game = CalendarGame(
        "psn:10012345",
        "光与影：33号远征队 (泰语, 日语, 简体中文)",
        date(2025, 4, 24),
        "https://ps.example/cover.jpg",
        "PS",
    )
    steam_game = CalendarGame(
        "2694490",
        "光与影：33号远征队",
        date(2025, 4, 24),
        "https://steam.example/cover.jpg",
        "Steam",
    )

    assert registry.add(ps_game, "100") is True
    created, saved = registry.add_resolved(steam_game, "200")

    assert created is False
    assert saved == steam_game
    assert registry.list_month(2025, 4) == [steam_game]


def test_ps_duplicate_does_not_replace_existing_steam_version(tmp_path: Path) -> None:
    registry = GameCalendarRegistry(tmp_path / "calendar.db")
    steam_game = CalendarGame("1", "Example Game", date(2026, 9, 17), None, "Steam")
    ps_game = CalendarGame(
        "psn:2", "Example Game (英语, 日语)", date(2026, 9, 18), None, "PS"
    )
    registry.add(steam_game, "100")

    created, saved = registry.add_resolved(ps_game, "200")

    assert created is False
    assert saved == steam_game
    assert registry.list_month(2026, 9) == [steam_game]


def test_similar_name_with_distant_release_date_is_not_merged(tmp_path: Path) -> None:
    registry = GameCalendarRegistry(tmp_path / "calendar.db")
    first = CalendarGame("1", "Example Game", date(2026, 1, 1), None, "Steam")
    second = CalendarGame("psn:2", "Example Game", date(2026, 2, 1), None, "PS")

    assert registry.add(first, "100") is True
    assert registry.add(second, "200") is True
    assert registry.list_all() == [first, second]


def test_language_suffix_is_ignored_for_duplicate_name_comparison() -> None:
    assert game_names_highly_similar(
        "光与影：33号远征队",
        "光与影：33号远征队 (泰语, 日语, 简体中文)",
    )


def test_similar_sequel_names_are_not_duplicates() -> None:
    assert not game_names_highly_similar("Nioh 2", "Nioh 3")
    assert not game_names_highly_similar("Rogue Fable III", "Rogue Fable IV")


def test_steam_matcher_skips_soundtrack_and_uses_exact_release_date() -> None:
    class FakeClient:
        async def _public_get(self, _url: str, **_params: str):
            return {
                "items": [
                    {"type": "app", "id": 10, "name": "Example Soundtrack"},
                    {"type": "app", "id": 20, "name": "Example Game"},
                ]
            }

        async def game_details(self, app_id: str):
            if app_id == "10":
                return {"type": "music", "name": "Example Soundtrack"}
            return {
                "type": "game",
                "name": "示例游戏",
                "capsule_image": "https://example.com/cover.jpg",
                "release_date": {"coming_soon": True, "date": "2026 年 9 月 18 日"},
            }

    game = asyncio.run(SteamGameMatcher(FakeClient()).lookup("示例游戏"))  # type: ignore[arg-type]

    assert game.app_id == "20"
    assert game.name == "示例游戏"
    assert game.release_date == date(2026, 9, 18)
    assert game.image_url == "https://example.com/cover.jpg"


def test_steam_matcher_prefers_china_date_from_precise_release_timestamp() -> None:
    class FakeClient:
        async def _public_get(self, _url: str, **_params: str):
            return {"items": [{"type": "app", "id": 4225980, "name": "空之轨迹 the 2nd"}]}

        async def game_details(self, _app_id: str):
            return {
                "type": "game",
                "name": "空之轨迹 the 2nd",
                "release_date": {"coming_soon": True, "date": "2026 年 9 月 16 日"},
            }

        async def _public_text(self, _url: str, **params: str) -> str:
            assert params == {"l": "schinese", "cc": "cn"}
            return 'app_release_date&quot;:&quot;1789606800&quot;'

    game = asyncio.run(
        SteamGameMatcher(FakeClient()).lookup("空之轨迹 the 2nd")  # type: ignore[arg-type]
    )

    assert game.app_id == "4225980"
    assert game.release_date == date(2026, 9, 17)


def test_month_selector_uses_next_occurrence() -> None:
    today = date(2026, 8, 31)
    assert parse_month_selector("9", today) == (2026, 9)
    assert parse_month_selector("7", today) == (2027, 7)
    assert parse_month_selector("2028-2", today) == (2028, 2)


def test_calendar_renderer_outputs_vertical_png() -> None:
    games = [
        CalendarGame("1", "示例游戏 A", date(2026, 9, 3), None),
        CalendarGame("2", "Example Game B", date(2026, 9, 18), None),
    ]
    content = _render_calendar(2026, 9, games, {})

    with Image.open(BytesIO(content)) as image:
        assert image.format == "PNG"
        assert image.width == 760
        assert image.height > 300


def test_year_months_start_at_current_month_only_for_current_year() -> None:
    assert calendar_year_months(2026, date(2026, 9, 17)) == [9, 10, 11, 12]
    assert calendar_year_months(2026, date(2026, 12, 31)) == [12]
    assert calendar_year_months(2027, date(2026, 9, 17)) == list(range(1, 13))


def test_year_calendar_stitches_months_at_top_and_preserves_empty_months() -> None:
    games = [
        CalendarGame("1", "September", date(2026, 9, 3), None),
        CalendarGame("2", "October", date(2026, 10, 18), None),
        CalendarGame("3", "October Second", date(2026, 10, 20), None),
        CalendarGame("4", "Excluded", date(2027, 9, 3), None),
    ]
    with Image.open(BytesIO(_render_year_calendar(2026, [9, 10, 11, 12], games, {}))) as combined:
        assert combined.width == 760 * 4
        with Image.open(BytesIO(_render_calendar(2026, 10, games[1:3], {}))) as october:
            assert combined.height == october.height
            assert combined.crop((760, 0, 1520, october.height)).tobytes() == october.tobytes()
        with Image.open(BytesIO(_render_calendar(2026, 9, games[:1], {}))) as september:
            assert combined.crop((0, 0, 760, september.height)).tobytes() == september.tobytes()
        assert combined.getpixel((0, combined.height - 1)) == (8, 19, 29)


def test_empty_year_calendar_returns_one_image() -> None:
    message = asyncio.run(build_year_calendar_message(2026, [9, 10, 11, 12], []))
    assert len(message) == 1
    assert message[0].type == "image"


def test_calendar_cover_preserves_entire_wide_image() -> None:
    source = BytesIO()
    Image.new("RGB", (462, 120), "#e60012").save(source, format="PNG")

    cover = _calendar_cover(source.getvalue(), (231, 87))

    assert cover.size == (231, 87)
    assert cover.getpixel((115, 43)) == (230, 0, 18)
    assert cover.getpixel((115, 2)) != (230, 0, 18)


def test_uploaded_calendar_cover_crops_to_fill_entire_area() -> None:
    source = BytesIO()
    Image.new("RGB", (120, 360), "#006fcd").save(source, format="PNG")

    cover = _calendar_cover(source.getvalue(), (231, 87), fill=True)

    assert cover.size == (231, 87)
    assert cover.getpixel((2, 2)) == (0, 111, 205)
    assert cover.getpixel((228, 84)) == (0, 111, 205)


def test_remote_store_cover_fills_calendar_image_slot() -> None:
    source = BytesIO()
    Image.new("RGB", (160, 90), "#006fcd").save(source, format="PNG")
    game = CalendarGame(
        "psn:10000000",
        "PS Store Game",
        date(2026, 9, 20),
        "https://image.api.playstation.com/cover.png",
        "PS",
    )

    content = _render_calendar(2026, 9, [game], {game.image_url: source.getvalue()})

    with Image.open(BytesIO(content)) as rendered:
        # This point used to be the dark letterbox beside a 16:9 PS image.
        assert rendered.convert("RGB").getpixel((58, 220)) == (0, 111, 205)


def test_calendar_message_reads_admin_uploaded_local_image(
    tmp_path: Path, monkeypatch,
) -> None:
    from qq_bot import game_calendar

    image_dir = tmp_path / "images"
    image_dir.mkdir()
    Image.new("RGB", (300, 160), "#006fcd").save(image_dir / "game.jpg", format="JPEG")
    monkeypatch.setattr(game_calendar, "LOCAL_IMAGE_DIR", image_dir)
    game = CalendarGame(
        "manual-1",
        "PS 独占游戏",
        date(2026, 9, 20),
        "local:game.jpg",
        "PS",
    )

    message = asyncio.run(build_calendar_message(2026, 9, [game]))

    assert message[0].type == "image"
    assert str(message[0].data["file"]).startswith("base64://")


def test_release_reminder_message_is_an_image() -> None:
    target = date(2026, 9, 17)
    message = asyncio.run(
        build_release_reminder_message(
            target,
            1,
            [CalendarGame("1", "Example Game", target, None)],
        )
    )

    assert message[0].type == "image"
    assert str(message[0].data["file"]).startswith("base64://")

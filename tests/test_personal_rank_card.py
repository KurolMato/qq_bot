from datetime import datetime, timezone
from io import BytesIO

from PIL import Image, ImageDraw

from qq_bot import personal_rank_card as card
from qq_bot.game_time_tracker import TrackedGame
from qq_bot.personal_rank_data import PersonalRankData


def png_bytes(size: tuple[int, int], colour: tuple[int, int, int, int] = (70, 110, 145, 255)) -> bytes:
    output = BytesIO()
    Image.new("RGBA", size, colour).save(output, format="PNG")
    return output.getvalue()


def game(index: int, image_url: str | None, *, seconds: int = 3600) -> TrackedGame:
    return TrackedGame("Steam", f"Game {index}", seconds, image_url)


def base_data(**overrides):
    result = {
        "name": "A long player name 星野",
        "avatar_url": "avatar",
        "day_seconds": 5400,
        "week_seconds": 28800,
        "month_seconds": 72000,
        "games": (),
        "recent_games": (),
        "platinum_count": 2,
        "perfect_count": None,
        "achievements": {},
        "as_of": datetime(2026, 9, 26, 12, tzinfo=timezone.utc),
    }
    result.update(overrides)
    return result


def test_finalized_month_header_uses_gaming_month_not_calendar_date(monkeypatch):
    labels = []
    original = card._text

    def record(draw, x, y, value, *args, **kwargs):
        labels.append(value)
        return original(draw, x, y, value, *args, **kwargs)

    monkeypatch.setattr(card, '_text', record)
    data = base_data(as_of=datetime(2026, 9, 30, 19, 59, 59, 999999, tzinfo=timezone.utc), finalized_month=True)
    content = card.render_personal_rank(data, {})
    assert '2026年09月 · 月末结算' in labels
    with Image.open(BytesIO(content)) as output:
        assert output.size == card.CANVAS_SIZE


def test_cover_validation_accepts_only_decoded_landscape_images():
    landscape = card._decoded_asset(png_bytes((640, 360)), landscape=True)
    portrait = card._decoded_asset(png_bytes((360, 640)), landscape=True)
    corrupt = card._decoded_asset(b"not an image", landscape=True)
    transparent = card._decoded_asset(png_bytes((640, 360), (0, 0, 0, 0)), landscape=True)

    assert landscape is not None and landscape.width > landscape.height
    assert portrait is None
    assert corrupt is None
    assert transparent is None


def test_switch_square_art_fills_frame_with_center_crop():
    switch = TrackedGame("switch", "Nintendo game", 600, "square")
    source = Image.new("RGB", (100, 100), "red")
    draw = ImageDraw.Draw(source)
    draw.rectangle((0, 0, 99, 9), fill="blue")
    draw.rectangle((0, 90, 99, 99), fill="blue")
    tile = card._fit_game_art(source, (320, 180), switch)
    assert tile.size == (320, 180)
    assert all(tile.getpixel(position) == (255, 0, 0) for position in [(0, 0), (319, 0), (0, 179), (319, 179)])
    assert len(card._available_month_covers((switch,), {'square': png_bytes((100, 100))})) == 1


def test_month_collage_skips_bad_covers_and_reports_all_omitted_games(monkeypatch):
    games = tuple(game(i, "wide" if i < 5 else f"missing-{i}") for i in range(8))
    assets = {"wide": png_bytes((640, 360))}
    available = card._available_month_covers(games, assets)
    assert [entry[0].name for entry in available] == [f"Game {i}" for i in range(5)]
    assert len(card._tile_positions(5)) == 5
    positions = card._tile_positions(5)
    assert positions[3][0] > positions[0][0]
    assert positions[3][1] == positions[4][1] == 746

    drawn = []
    original_fitted = card._fitted

    def record_fitted(draw, x, y, value, *args, **kwargs):
        drawn.append(str(value))
        return original_fitted(draw, x, y, value, *args, **kwargs)

    monkeypatch.setattr(card, "_fitted", record_fitted)
    image = Image.new("RGBA", card.CANVAS_SIZE, card.BG)
    card._draw_collage(image, ImageDraw.Draw(image), base_data(month_seconds=72000), games, assets)

    assert any("共 8 款游戏" in value for value in drawn)
    assert any("展示 5 张封面 · 另有 3 款未展示" in value for value in drawn)


def test_render_uses_text_only_states_for_missing_or_portrait_images(monkeypatch):
    games = (game(0, "portrait"), game(1, "broken"))
    assets = {
        "avatar": b"broken avatar bytes",
        "portrait": png_bytes((360, 640)),
        "broken": b"broken cover bytes",
    }
    labels = []
    original_text = card._text
    original_fitted = card._fitted

    def record_text(draw, x, y, value, *args, **kwargs):
        labels.append(str(value))
        return original_text(draw, x, y, value, *args, **kwargs)

    def record_fitted(draw, x, y, value, *args, **kwargs):
        labels.append(str(value))
        return original_fitted(draw, x, y, value, *args, **kwargs)

    monkeypatch.setattr(card, "_text", record_text)
    monkeypatch.setattr(card, "_fitted", record_fitted)

    content = card.render_personal_rank(
        base_data(games=games, recent_games=(games[0],)), assets
    )
    with Image.open(BytesIO(content)) as result:
        result.load()
        assert result.format == "PNG"
        assert result.size == (1500, 1000)

    assert "本月暂无可用的横版封面" in labels
    assert "游戏时长仍计入本月累计" in labels
    assert "Steam   /   本周" in labels
    assert "本月累计 1 小时" in labels


def test_render_reads_frozen_personal_data_and_uses_local_date(monkeypatch):
    games = (TrackedGame("steam", "Month title", 3600, None),)
    recent = TrackedGame("steam", "Month title", 1200, None)
    profile = PersonalRankData(
        "Tester", None, 600, 7200, 10800, games, (recent,), None, 0, {},
        datetime(2026, 9, 25, 18, 30, tzinfo=timezone.utc),
    )
    drawn = []
    original_text = card._text
    original_fitted = card._fitted

    def record_text(draw, x, y, value, *args, **kwargs):
        drawn.append(str(value))
        return original_text(draw, x, y, value, *args, **kwargs)

    def record_fitted(draw, x, y, value, *args, **kwargs):
        drawn.append(str(value))
        return original_fitted(draw, x, y, value, *args, **kwargs)

    monkeypatch.setattr(card, "_text", record_text)
    monkeypatch.setattr(card, "_fitted", record_fitted)

    content = card.render_personal_rank(profile, {})
    with Image.open(BytesIO(content)) as result:
        result.load()
        assert result.size == (1500, 1000)

    assert "Tester" in drawn
    assert "2026.09.26" in drawn
    assert "10 分" in drawn
    assert "2 小时" in drawn
    assert "3 小时" in drawn
    assert "Steam   /   本周" in drawn
    assert "本月累计 1 小时" in drawn


def test_platform_aliases_and_long_durations_fit_text_boxes():
    assert card._platform_label("steam") == "Steam"
    assert card._platform_label("ps5", short=True) == "PS"
    assert card._platform_label("ps5") == "PlayStation"
    assert card._platform_label("switch") == "Switch"

    image = Image.new("RGB", (1500, 1000), card.BG)
    draw = ImageDraw.Draw(image)
    value = card._duration(10**14)
    fitted, font = card._fitted_text(draw, value, 286, 29, 14)
    bounds = draw.textbbox((0, 0), fitted, font=font)
    assert bounds[2] - bounds[0] <= 286


def test_completion_count_pairs_center_by_measured_glyph_bounds():
    class Recorder:
        def __init__(self, draw):
            self.draw = draw
            self.bounds = {}
            self.fonts = {}

        def textbbox(self, xy, text, font):
            return self.draw.textbbox(xy, text, font=font)

        def text(self, xy, text, *, font, fill):
            self.bounds[text] = self.draw.textbbox(xy, text, font=font)
            self.fonts[text] = font
            self.draw.text(xy, text, font=font, fill=fill)

    recorder = Recorder(ImageDraw.Draw(Image.new("RGB", (400, 100), "#151F29")))
    card._draw_count_label(recorder, 143, "123456", "白金")
    card._draw_count_label(recorder, 278, "—", "全成就")

    for count, label, center in (("123456", "白金", 143), ("—", "全成就", 278)):
        count_box = recorder.bounds[count]
        label_box = recorder.bounds[label]
        left = min(count_box[0], label_box[0])
        right = max(count_box[2], label_box[2])
        assert abs((left + right) / 2 - center) <= 1
        assert right - left <= 124
    assert recorder.fonts["123456"].size < 33

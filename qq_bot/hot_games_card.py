"""Offline Pillow renderer for the approved smoked-glass popularity card."""
from io import BytesIO

from PIL import Image, ImageDraw, ImageOps

from .personal_rank_card import _glass_panel, _rounded_paste, _decoded_asset
from .switch_presence import _font, _fitted_text
from .hot_games import hot_game_ranks

METALS = (
    ((185, 155, 96), (255, 244, 213), (97, 80, 56)),
    ((151, 169, 188), (244, 248, 255), (69, 85, 104)),
    ((179, 126, 92), (255, 225, 201), (98, 67, 49)),
)


def _metal_rim(canvas, box, place):
    x, y, r, b = box
    w, h = r - x, b - y
    mask = Image.new("L", (w * 2, h * 2))
    ImageDraw.Draw(mask).rounded_rectangle((1, 1, w * 2 - 2, h * 2 - 2), 44,
                                          outline=255, width=4)
    mask = mask.resize((w, h), Image.Resampling.LANCZOS)
    mid, light, dark = METALS[place]
    stops = [mid, light, mid, dark, mid, light, dark]
    ramp = Image.new("RGB", (w + h, 1))
    colors = []
    for i in range(w + h):
        value = i / (w + h - 1) * (len(stops) - 1)
        index = min(int(value), len(stops) - 2)
        t = value - index
        colors.append(tuple(round(a + (b - a) * t) for a, b in zip(stops[index], stops[index + 1])))
    ramp.putdata(colors)
    rim = Image.new("RGB", (w, h))
    for row in range(h):
        rim.paste(ramp.crop((row, 0, row + w, 1)), (0, row))
    canvas.paste(rim, (x, y), mask)


def render_hot_games(data, assets) -> bytes:
    rows = data.games
    ranks = hot_game_ranks(rows)
    height = 510 + max(0, len(rows) - 3) * 94 if rows else 330
    canvas = Image.new("RGB", (900, height), "#0C131A")
    draw = ImageDraw.Draw(canvas)

    def text(x, y, value, size=22, color="#F2F5F7", width=None):
        font = _font(size)
        if width is not None:
            value, font = _fitted_text(draw, value, width, size, size)
        draw.text((x, y), value, font=font, fill=color, anchor="lt")

    def cover(game, box, radius=14, shade=False):
        x, y, r, b = box
        source = _decoded_asset(assets.get(game.image_url))
        if source is None:
            source = Image.new("RGB", (r - x, b - y), "#243847")
        tile = ImageOps.fit(source.convert("RGB"), (r - x, b - y), method=Image.Resampling.LANCZOS)
        if shade:
            overlay = Image.new("RGBA", tile.size)
            od = ImageDraw.Draw(overlay)
            for line in range(tile.height):
                alpha = int(245 * (line / max(1, tile.height - 1)) ** 1.4)
                od.line((0, line, tile.width, line), fill=(9, 18, 29, alpha))
            tile = Image.alpha_composite(tile.convert("RGBA"), overlay)
        _rounded_paste(canvas, tile, (x, y), radius)

    def count(x, y, n, size=36):
        value = str(n)
        text(x, y, value, size)
        offset = draw.textlength(value, font=_font(size))
        text(x + offset + 8, y + size - 18, "人游玩", 16, "#B4C5D4")

    def title_lines(value, width, size):
        first = ""
        while value and draw.textlength(first + value[0], font=_font(size)) <= width:
            first += value[0]
            value = value[1:]
        return first, value

    text(40, 38, "月热游" if data.period == "month" else "周热游", 36)
    _glass_panel(canvas, (748, 34, 860, 78), 22)
    text(780, 47, "本月" if data.period == "month" else "本周", 18)
    if not rows:
        _glass_panel(canvas, (40, 118, 860, 286), 22)
        text(260, 185, "本期暂无可统计的游玩记录", 24)
    else:
        winner = rows[0]
        box = (40, 112, 470, 468)
        _glass_panel(canvas, box, 22)
        cover(winner, box, 22, shade=True)
        _metal_rim(canvas, box, 0)
        draw.rounded_rectangle((62, 134, 208, 174), 20, fill="#302C25", outline="#B99B60")
        text(79, 144, "01 · 人气榜首", 17, "#F4DFAA")
        # Two lines preserve readable titles, with ellipsis for extreme names.
        first, name = title_lines(winner.name, 374, 28)
        text(66, 325 if name else 349, first, 28)
        if name:
            text(66, 361, name, 25, width=374)
        count(66, 407, winner.players, 40)
        for index, game in enumerate(rows[1:3], 1):
            y = 112 + (index - 1) * 186
            box = (490, y, 860, y + 170)
            _glass_panel(canvas, box, 22)
            cover(game, (510, y + 24, 630, y + 146))
            rank = ranks[index]
            _metal_rim(canvas, box, rank - 1)
            text(649, y + 22, f"{rank:02}", 18, METALS[rank - 1][1])
            first, rest = title_lines(game.name, 190, 21)
            text(649, y + 52, first, 21)
            if rest:
                text(649, y + 80, rest, 21, width=190)
            count(649, y + 119, game.players, 30)
        if len(rows) > 3:
            _glass_panel(canvas, (40, 490, 860, height - 20), 22)
        for index, game in enumerate(rows[3:], 4):
            y = 506 + (index - 4) * 94
            rank = ranks[index - 1]
            if index > 4:
                draw.line((62, y - 8, 838, y - 8), fill="#293642")
            if rank <= 3:
                _metal_rim(canvas, (48, y - 3, 852, y + 77), rank - 1)
            text(64, y + 22, f"{rank:02}", 18, METALS[rank - 1][1] if rank <= 3 else "#8FA4B5")
            cover(game, (106, y + 4, 222, y + 69), 10)
            text(246, y + 22, game.name, 22, width=420)
            count(716, y + 21, game.players, 28)
    output = BytesIO()
    canvas.save(output, format="PNG")
    return output.getvalue()

"""Render an isolated /rank me design proposal with illustrative data.

This does not import bot services or read/write runtime databases.
Assets are read from the preview folder; --collect-assets copies selected cached
artwork on the design machine to make subsequent renders self-contained.
"""
from pathlib import Path
import argparse
import math

from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "assets" / "previews"
ART = OUT / "rank-me-assets"
SOURCES = {
    "avatar": "5e235f44", "zelda": "de9f0fe6", "persona": "c7b2b34",
    "crimson": "0de7feb", "fire-emblem": "c2801e", "gta": "fd7016",
    "survivor": "0de43a11",
}
BG, PANEL, LINE = "#0C131A", "#151F29", "#293642"
WHITE, MUTED, MINT = "#F2F5F7", "#93A6B6", "#A9E7D3"
GAME_TILE_SIZE = (324, 182)  # 16:9 landscape cover fitted to every collage frame.


def font(size, bold=False, latin=False):
    fonts = Path("C:/Windows/Fonts")
    path = fonts / ("segoeuib.ttf" if bold else "segoeui.ttf") if latin else fonts / ("msyhbd.ttc" if bold else "msyh.ttc")
    return ImageFont.truetype(str(path), size)


def achievement_icon(kind, size=56):
    """Code-rendered silver-blue platinum cup or gold-and-blue completion ribbon."""
    icon = Image.new("RGBA", (192, 192))
    draw = ImageDraw.Draw(icon)
    if kind == "白金":
        # Silver handles, blue cup and faceted platinum centre.
        draw.arc((15, 26, 104, 137), 80, 275, fill="#374C77", width=18)
        draw.arc((88, 26, 177, 137), 265, 460, fill="#374C77", width=18)
        draw.arc((19, 23, 102, 133), 80, 275, fill="#BDCEE9", width=9)
        draw.arc((90, 23, 173, 133), 265, 460, fill="#BDCEE9", width=9)
        draw.polygon([(78, 115), (114, 115), (108, 154), (133, 168), (133, 178), (59, 178), (59, 168), (84, 154)], fill="#B4C8E3")
        draw.polygon([(90, 120), (102, 120), (101, 160), (121, 170), (75, 170), (92, 158)], fill="#F0F6FF")
        draw.ellipse((40, 17, 151, 130), fill="#526FA2", outline="#DEEAFC", width=5)
        draw.ellipse((50, 23, 141, 116), fill="#89ABD3")
        draw.polygon([(96, 25), (126, 49), (119, 85), (89, 119), (100, 77), (74, 55)], fill="#EDF8FF")
        draw.polygon([(96, 25), (74, 55), (100, 77), (86, 88), (65, 63)], fill="#BDD8ED")
        draw.polygon([(126, 49), (119, 85), (89, 119), (109, 76)], fill="#5C7DB7")
        draw.arc((55, 24, 136, 113), 145, 270, fill="#F7FAFF", width=5)
    else:
        draw.polygon([(52, 99), (87, 112), (77, 177), (57, 157), (34, 164)], fill="#6DC4F1", outline="#285D91", width=5)
        draw.polygon([(105, 111), (140, 99), (159, 164), (132, 157), (113, 177)], fill="#6DC4F1", outline="#285D91", width=5)
        points = []
        for i in range(28):
            a = -math.pi/2 + i*math.pi/14
            radius = 69 if i % 2 == 0 else 55
            points.append((96+math.cos(a)*radius, 78+math.sin(a)*radius))
        draw.polygon(points, fill="#6DC4F1", outline="#285D91", width=5)
        draw.ellipse((53, 35, 139, 121), fill="#FFD42F", outline="#DDA51C", width=5)
        draw.arc((60, 42, 132, 115), 200, 290, fill="#FFE784", width=4)
    return icon.resize((size, size), Image.Resampling.LANCZOS)


def achievement_frame(kind, size):
    """Transparent, antialiased UI frame; all decoration stays inside the tile."""
    scale = 3
    w, h = size
    big = (w*scale, h*scale)
    mask = Image.new("L", big)
    md = ImageDraw.Draw(mask)
    md.rounded_rectangle((0, 0, big[0]-1, big[1]-1), 12*scale, fill=255)
    md.rounded_rectangle((5*scale, 5*scale, big[0]-1-5*scale, big[1]-1-5*scale), 8*scale, fill=0)
    if kind == "白金":
        stops = [(74, 119, 175), (215, 236, 255), (132, 174, 213), (244, 250, 255), (94, 141, 194)]
    else:
        stops = [(58, 155, 219), (123, 207, 235), (255, 229, 125), (244, 187, 53), (255, 223, 109)]
    # Diagonal sheen gives opposite corners distinct cool/warm highlights.
    ramp = Image.new("RGB", (big[0]+big[1], 1))
    pixels = []
    for p in range(ramp.width):
        value = p/(ramp.width-1)*(len(stops)-1)
        index = min(int(value), len(stops)-2)
        mix = value-index
        pixels.append(tuple(round(a+(b-a)*mix) for a, b in zip(stops[index], stops[index+1])))
    ramp.putdata(pixels)
    frame = Image.new("RGBA", big)
    for y in range(big[1]):
        frame.paste(ramp.crop((y, 0, y+big[0], 1)), (0, y))
    frame.putalpha(mask)
    fd = ImageDraw.Draw(frame)
    if kind == "白金":
        # Engraved inner corner brackets and a small platinum facet.
        for points in [[(w-43, 9), (w-13, 9), (w-9, 13), (w-9, 35)],
                       [(9, h-35), (9, h-13), (13, h-9), (43, h-9)]]:
            fd.line([(x*scale, y*scale) for x, y in points], fill="#D9EFFF", width=scale)
        cx, cy = (w-29)*scale, 22*scale
        fd.polygon([(cx, cy-6*scale), (cx+4*scale, cy), (cx, cy+6*scale), (cx-4*scale, cy)], fill="#F2F9FF")
    else:
        # Gold double corner accents echo the completion ribbon.
        for offset in (0, 7):
            points = [(w-45+offset, 9), (w-19+offset, 9), (w-9, 19-offset)]
            fd.line([(x*scale, y*scale) for x, y in points], fill="#FFE694", width=2*scale)
        fd.line([(10*scale, (h-13)*scale), (16*scale, (h-9)*scale), (44*scale, (h-9)*scale)], fill="#FFE694", width=2*scale)
    return frame.resize(size, Image.Resampling.LANCZOS)


def render():
    canvas = Image.new("RGB", (1500, 1000), BG)
    glow = Image.new("RGBA", canvas.size)
    gd = ImageDraw.Draw(glow)
    gd.ellipse((-200, -260, 650, 550), fill=(48, 102, 103, 38))
    canvas = Image.alpha_composite(canvas.convert("RGBA"), glow.filter(ImageFilter.GaussianBlur(100))).convert("RGB")
    d = ImageDraw.Draw(canvas)

    def text(x, y, value, size=22, color=WHITE, bold=False, latin=False):
        d.text((x, y), value, font=font(size, bold, latin), fill=color, anchor="lt")

    def right(x, y, value, size=22, color=MUTED, latin=False):
        d.text((x, y), value, font=font(size, latin=latin), fill=color, anchor="rt")

    def panel(box):
        d.rounded_rectangle(box, 24, fill=PANEL, outline=LINE, width=1)

    def paste(img, x, y, radius=16):
        mask = Image.new("L", img.size)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, img.width-1, img.height-1), radius, fill=255)
        canvas.paste(img.convert("RGB"), (x, y), mask)

    def art(name, size):
        with Image.open(ART / f"{name}.png") as source:
            return ImageOps.fit(source.convert("RGB"), size, method=Image.Resampling.LANCZOS)

    def shade(img, strength=230):
        layer = Image.new("RGBA", img.size)
        ld = ImageDraw.Draw(layer)
        for y in range(img.height):
            alpha = int(strength * (y / max(1, img.height-1)) ** 2)
            ld.line((0, y, img.width, y), fill=(5, 12, 18, alpha))
        return Image.alpha_composite(img.convert("RGBA"), layer)

    right(1452, 43, "SEPTEMBER 2026   /   09.25", 18, latin=True)

    # Top-left identity block.
    panel((48, 112, 372, 455))
    paste(art("avatar", (204, 204)), 108, 138, 30)
    name = "星野"
    d.text((210, 361), name, font=font(32, True), fill=WHITE, anchor="mt")
    d.text((210, 408), "Steam  ·  PlayStation  ·  Switch", font=font(16, latin=True), fill=MUTED, anchor="mt")

    # Top-right landscape artwork, with legible information over its lower edge.
    hero = shade(art("zelda", (1056, 343)), 246)
    paste(hero, 396, 112, 24)
    d.rounded_rectangle((424, 136, 594, 174), 19, fill="#162C2B")
    d.ellipse((440, 151, 447, 158), fill=MINT)
    text(459, 144, "近期常玩", 19, MINT)
    text(426, 333, "塞尔达传说：旷野之息", 35, bold=True)
    text(427, 391, "Nintendo Switch   /   最近 7 天", 19, "#C5D0D8")
    right(1419, 333, "12 小时 30 分", 29, WHITE)
    right(1419, 393, "本月累计 42 小时 15 分", 18, "#C5D0D8")

    # Bottom-left summary: consistent units and a separate completion section.
    panel((48, 479, 372, 958))
    text(76, 504, "游玩概览", 23, bold=True)
    for label, value, y in [("今日游玩", "3 小时 20 分", 558), ("本周游玩", "18 小时 45 分", 652), ("本月游玩", "128 小时 40 分", 746)]:
        text(76, y, label, 19, MUTED)
        text(76, y+32, value, 31, WHITE, True)
    d.line((76, 837, 344, 837), fill=LINE)
    text(76, 858, "本月白金 / 全成就", 19, MUTED)
    text(76, 894, "1", 33, MINT, True, True)
    text(106, 897, "白金", 18)
    text(217, 894, "1", 33, MINT, True, True)
    text(247, 897, "全成就", 18)

    # Bottom-right 3 × 2 landscape cover collage. All game data is illustrative.
    panel((396, 479, 1452, 958))
    text(424, 503, "本月游戏拼图", 24, bold=True)
    right(1424, 508, "6 款游戏  ·  128 小时 40 分", 19)
    games = [
        ("zelda", "旷野之息", "Switch", "42h 15m", ""),
        ("persona", "女神异闻录 5 皇家版", "PS", "28h 30m", "白金"),
        ("crimson", "红色沙漠", "Steam", "23h 45m", ""),
        ("fire-emblem", "火焰纹章 风花雪月", "Switch", "16h 20m", ""),
        ("gta", "Grand Theft Auto V", "Steam", "11h 10m", "全成就"),
        ("survivor", "幸存者笔记", "Steam", "6h 40m", ""),
    ]
    for i, (key, title, platform, duration, achievement) in enumerate(games):
        x, y = 424 + (i % 3)*338, 554 + (i // 3)*192
        tile = shade(art(key, GAME_TILE_SIZE), 252)
        paste(tile, x, y, 12)
        if achievement:
            frame = achievement_frame(achievement, tile.size)
            canvas.paste(frame, (x, y), frame)
            badge = achievement_icon(achievement)
            canvas.paste(badge, (x+12, y+10), badge)
        text(x+15, y+115, title, 21, bold=True)
        text(x+15, y+151, platform, 16, "#C5D0D8", latin=True)
        right(x+309, y+149, duration, 18, WHITE, True)

    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "rank-me-ui-preview.png"
    canvas.save(path, optimize=True)
    print(path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--collect-assets", action="store_true")
    args = parser.parse_args()
    if args.collect_assets:
        ART.mkdir(parents=True, exist_ok=True)
        for name, prefix in SOURCES.items():
            source = next((ROOT / "data" / "list_avatar_cache").glob(prefix + "*.img"))
            with Image.open(source) as im:
                im.convert("RGB").save(ART / f"{name}.png")
    render()

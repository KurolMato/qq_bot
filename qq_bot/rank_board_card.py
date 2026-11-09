"""Unified glass leaderboard layout; all inputs and images are supplied offline."""
from io import BytesIO
from math import ceil

from PIL import Image, ImageDraw

from .hot_games_card import METALS, _metal_rim
from .personal_rank_card import _glass_panel
from .list_cards import _paste_rounded, _source_image, _render_fallback_mark
from .switch_presence import _font, _fitted_text


def render_rank_board(snapshot, assets, personal_name=None):
    from .game_time_leaderboard import PLATFORM, _duration, _visible_games, _centred

    groups = [_visible_games(member, snapshot, personal_name) for member in snapshot.members]
    # The leader gets a cover grid. Everyone else keeps every game in compact rows.
    heights = [110 + (ceil(len(games) / 3) * 220 if index == 0 else len(games) * 82)
               if games else 154 for index, games in enumerate(groups)]
    height = max(300, 156 + sum(heights) + 16 * len(heights))
    canvas = Image.new("RGB", (900, height), "#0C131B")
    draw = ImageDraw.Draw(canvas)

    def text(x, y, value, size=22, color="#EEF3F7", width=None):
        font = _font(size)
        if width is not None:
            value, font = _fitted_text(draw, value, width, size, max(13, size - 4))
        draw.text((x, y), value, font=font, fill=color, anchor="lt")

    def right(x, y, value, size=22, width=240):
        value, font = _fitted_text(draw, value, width, size, 18)
        draw.text((x, y), value, font=font, fill="#EEF3F7", anchor="rt")

    def badge(x, y, game):
        label, color = PLATFORM.get(game.platform.casefold(), (game.platform, "#526c80"))
        draw.rounded_rectangle((x, y, x + 72, y + 26), 8, fill=color)
        label, font = _fitted_text(draw, label, 64, 13, 11)
        _centred(draw, (x, y, x + 72, y + 26), label, font, "#FFFFFF")
        return color

    def bar(x, y, width, game, maximum, color):
        draw.rounded_rectangle((x, y, x + width, y + 4), 2, fill="#2B3B49")
        length = round(width * min(1, max(0, game.seconds / maximum))) if maximum > 0 else 0
        if length:
            draw.rounded_rectangle((x, y, x + length, y + 4), 2, fill=color)

    def cover(game, box):
        x, y, w, h = box
        image = _source_image(assets.get(game.image_url or ""), (w, h), "#263B4C")
        _paste_rounded(canvas, image, (x, y), 10)

    start = snapshot.day_start
    end = (snapshot.last_poll or start).astimezone(start.tzinfo)
    title = {"month": f"{start:%Y年%m月}游玩榜", "week": "本周游玩榜",
             "yesterday": "昨日游玩榜"}.get(snapshot.period, "今日游玩榜")
    if personal_name is not None:
        title = title.replace("游玩榜", "游戏时长")
    text(40, 36, title, 34, width=640)
    stamp = f"{start:%m月%d日} · 截至 {end:%H:%M}"
    if snapshot.period in {"week", "month", "yesterday"}:
        stamp = f"{start:%m月%d日}–{end:%m月%d日} · 截至 {end:%H:%M}"
    text(42, 88, stamp, 17, "#A6B7C6")
    _glass_panel(canvas, (744, 37, 860, 79), 21)
    text(766, 49, "游戏时长", 17)
    if not snapshot.members:
        _glass_panel(canvas, (40, 138, 860, 270), 22)
        text(65, 192, f"{personal_name}：暂无可统计的游戏时长" if personal_name else "暂无可统计的游戏时长", 24, width=770)

    top = 136
    for index, (member, games, panel_height) in enumerate(zip(snapshot.members, groups, heights)):
        _glass_panel(canvas, (40, top, 860, top + panel_height), 22)
        if index < 3 and personal_name is None:
            _metal_rim(canvas, (40, top, 860, top + panel_height), index)
        if personal_name is None:
            text(61, top + 37, f"{index + 1:02}", 23,
                 METALS[index][1] if index < 3 else "#8FA6B8", width=40)
        avatar = _source_image(assets.get(member.avatar_url or ""), (58, 58), "#344A5D")
        _paste_rounded(canvas, avatar, (112, top + 23), 17)
        if not assets.get(member.avatar_url or ""):
            _render_fallback_mark(draw, (112, top + 23, 170, top + 81), member.name, "#DBE9F2")
        text(188, top + 39, member.name, 26, width=340)
        right(835, top + 34, _duration(member.total_seconds), 34, 285)
        if not games:
            text(112, top + 105, "暂无超过1小时的游戏" if snapshot.period == "month" and personal_name is None
                 else "暂无游戏明细", 18, "#A6B7C6")
        maximum = max((g.seconds for g in games), default=0)
        for offset, game in enumerate(games):
            if index == 0:
                x = 64 + offset % 3 * 264
                y = top + 103 + offset // 3 * 220
                cover(game, (x, y, 244, 118))
                text(x, y + 130, game.name, 18, width=244)
                color = badge(x, y + 161, game)
                right(x + 244, y + 166, _duration(game.seconds), 16, 164)
                bar(x, y + 199, 244, game, maximum, color)
            else:
                y = top + 100 + offset * 82
                cover(game, (112, y, 80, 46))
                text(208, y + 1, game.name, 20, width=420)
                color = badge(208, y + 28, game)
                right(834, y + 9, _duration(game.seconds), 19, 190)
                bar(208, y + 63, 626, game, maximum, color)
        top += panel_height + 16
    output = BytesIO()
    canvas.save(output, format="PNG", optimize=True)
    return output.getvalue()

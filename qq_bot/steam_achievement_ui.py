"""Pure renderer for the approved Steam completion card; no network requests."""
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from io import BytesIO
from pathlib import Path
import math

from PIL import Image, ImageDraw, ImageOps

from .personal_rank_card import achievement_frame
from .switch_presence import _font

SIZE = (1400, 910)
WHITE, MUTED, CYAN, GOLD = '#eff5fc', '#8fa1b9', '#8bd8ec', '#f4d396'
LOCAL_TZ = timezone(timedelta(hours=8))


@lru_cache(maxsize=1)
def _badge():
    path = Path(__file__).resolve().parent.parent / 'assets' / 'steam-perfect-classic.png'
    with Image.open(path) as source:
        badge = source.convert('RGBA')
    # Silhouette clipping removes the reference's white canvas without altering its art.
    mask = Image.new('L', (447, 447))
    draw = ImageDraw.Draw(mask)
    draw.polygon([(224,0),(272,50),(333,50),(333,111),(375,155),(333,197),
                  (333,260),(273,260),(224,309),(175,260),(114,260),(114,199),
                  (72,155),(114,112),(114,50),(175,50)], fill=255)
    draw.polygon([(96,310),(150,310),(193,353),(138,445),(109,384),(54,384)], fill=255)
    draw.polygon([(297,310),(351,310),(393,384),(339,384),(309,445),(254,353)], fill=255)
    badge.putalpha(mask.resize(badge.size, Image.Resampling.LANCZOS))
    return badge.resize((104, 104), Image.Resampling.LANCZOS)


def _image(content, size):
    if content:
        try:
            with Image.open(BytesIO(content)) as image:
                return ImageOps.fit(ImageOps.exif_transpose(image).convert('RGB'), size,
                                    method=Image.Resampling.LANCZOS)
        except Exception:
            pass
    return Image.new('RGB', size, '#22364a')


def _paste(canvas, content, box, radius):
    x, y, w, h = box
    mask = Image.new('L', (w, h))
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w-1, h-1), radius, fill=255)
    canvas.paste(_image(content, (w, h)), (x, y), mask)


def _lines(draw, value, font, width, count):
    remaining = ' '.join(str(value or '').split())
    lines = []
    while remaining and len(lines) < count:
        line = ''
        while remaining and draw.textlength(line + remaining[0], font=font) <= width:
            line += remaining[0]
            remaining = remaining[1:]
        if not line:
            break
        if remaining and len(lines) == count-1:
            while line and draw.textlength(line + '…', font=font) > width:
                line = line[:-1]
            line += '…'
        lines.append(line)
    return lines


def _date(stamp):
    if type(stamp) not in (int, float) or not math.isfinite(stamp) or stamp <= 0:
        return '未记录'
    try:
        return datetime.fromtimestamp(stamp, LOCAL_TZ).strftime('%Y.%m.%d')
    except (OverflowError, OSError, ValueError):
        return '未记录'


def render_card(name, title, total, minutes, achieved_at, detected_at, avatar=None, cover=None,
                *, first_achieved_at=None, achievement_name=None, achievement_description=None,
                achievement_percent=None, achievement_icon=None, last_achievement_count=1):
    canvas = Image.new('RGB', SIZE, '#0b111b')
    draw = ImageDraw.Draw(canvas)

    def text(x, y, value, size, fill=WHITE, bold=False, anchor='lt'):
        draw.text((x, y), str(value), font=_font(size, bold=bold), fill=fill, anchor=anchor)

    def fitted(x, y, value, width, size, minimum, fill=WHITE, center_y=False):
        while size > minimum and draw.textlength(value, font=_font(size, bold=True)) > width:
            size -= 1
        font = _font(size, bold=True)
        value = (_lines(draw, value, font, width, 1) or [''])[0]
        if center_y:
            left, top, _, bottom = draw.textbbox((0,0), value, font=font)
            draw.text((x-left, y-(top+bottom)/2), value, font=font, fill=fill)
        else:
            draw.text((x,y), value, font=font, fill=fill, anchor='lt')

    def center(x, y, value, size, fill=WHITE):
        font = _font(size, bold=True)
        l, t, r, b = draw.textbbox((0,0), value, font=font)
        draw.text((x-(r+l)/2, y-(b+t)/2), value, font=font, fill=fill)

    draw.rounded_rectangle((20,20,1379,889), 30, fill='#111c2b', outline='#2a3b50', width=2)
    draw.rounded_rectangle((52,55,57,135), 2, fill=CYAN)
    text(77,51,'STEAM  /  PERFECT GAME',16,CYAN,True)
    text(75,82,'全成就达成',47,bold=True)
    badge = _badge()
    canvas.paste(badge, (1236,38), badge)
    draw.line((54,160,1346,160), fill='#2a3b50', width=2)
    _paste(canvas, cover, (91,190,360,540), 20)
    if not cover:
        center(271,460,'STEAM',36,MUTED)
    draw.rounded_rectangle((56,750,486,819), 15, fill='#19283b')
    _paste(canvas, avatar, (75,764,42,42), 21)
    if not avatar:
        center(96,785,'玩',20,CYAN)
    fitted(132,768,str(name),332,26,18)
    text(532,192,'STEAM ACHIEVEMENTS',17,MUTED,True)
    title_size = 39 if draw.textlength(title, font=_font(39,bold=True)) <= 812 else 31
    for i, line in enumerate(_lines(draw, title, _font(title_size,bold=True), 812, 2)):
        text(529,226+i*35,line,title_size,bold=True)

    draw.rounded_rectangle((532,301,1344,433),20,fill='#1a2b3d',outline='#30475b')
    text(587,320,'成就解锁',18,MUTED)
    fitted(587,380,f'{total} / {total}',138,37,19,center_y=True)
    draw.rounded_rectangle((733,360,823,400),10,fill='#2d4b4d')
    center(778,380,'100%',21,'#b5edce')
    draw.line((863,328,863,405),fill='#365065',width=1)
    text(1003,320,'累计游玩',18,MUTED)
    duration = '暂无数据' if type(minutes) is not int or minutes < 0 else f'{minutes//60}小时{minutes%60}分钟'
    fitted(1003,380,duration,311,31,20,CYAN,center_y=True)

    draw.rounded_rectangle((532,463,1344,609),14,fill='#1c2939',outline='#344256')
    _paste(canvas, achievement_icon, (555,493,86,86), 8)
    frame = achievement_frame('全成就', (96,96))
    canvas.paste(frame,(550,488),frame)
    if not achievement_icon:
        center(598,536,'★',35,GOLD)
    label = '最后解锁'
    if type(last_achievement_count) is int and last_achievement_count > 1:
        label += f' · 同时解锁{last_achievement_count}项'
    text(668,480,label,16,MUTED)
    fitted(668,511,achievement_name or '成就详情暂不可用',470,28,20)
    for i, line in enumerate(_lines(draw, achievement_description, _font(19), 470, 2)):
        text(668,551+i*24,line,19,'#b7c5d8')
    draw.line((1154,492,1154,580),fill='#3a4859',width=1)
    percent = '—'
    if type(achievement_percent) in (int,float) and math.isfinite(achievement_percent) and 0 <= achievement_percent <= 100:
        percent = '<0.1%' if 0 < achievement_percent < .1 else f'{achievement_percent:.1f}%'
    center(1247,525,percent,33,GOLD)
    text(1247,568,'全球玩家完成率',16,MUTED,anchor='mm')

    draw.rounded_rectangle((532,633,1344,839),20,fill='#142235',outline='#293d54')
    valid_end = _date(achieved_at) != '未记录'
    valid_start = _date(first_achieved_at) != '未记录' and valid_end and first_achieved_at <= achieved_at
    draw.rounded_rectangle((564,733,1312,739),3,fill='#4084c6' if valid_start else '#496075')
    draw.ellipse((556,728,572,744),fill='#98cbe8')
    draw.ellipse((1304,728,1320,744),fill=GOLD)
    text(564,685,'首个成就',18,MUTED)
    text(1312,685,'全成就达成' if valid_end else '检测到全成就',18,MUTED,anchor='rt')
    text(564,766,_date(first_achieved_at) if valid_start else '未记录',24,bold=True)
    text(1312,766,_date(achieved_at if valid_end else detected_at),24,bold=True,anchor='rt')
    # A compact single image keeps QQ upload work bounded.
    output = BytesIO()
    canvas.save(output, format='JPEG', quality=90, subsampling=0)
    return output.getvalue()

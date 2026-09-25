import asyncio
import logging
from datetime import datetime, timedelta, timezone
from io import BytesIO

from PIL import Image, ImageDraw
from nonebot.adapters.onebot.v11 import Message, MessageSegment

from .switch_presence import _card_image, _download_card_image, _fitted_text, _font, _rounded_paste

logger = logging.getLogger(__name__)


def render_card(name, title, total, minutes, achieved_at, detected_at, avatar=None, cover=None):
    canvas = Image.new('RGB', (1200, 800), '#0b131b')
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle((1, 1, 1198, 798), radius=30, fill='#141e28', outline='#3c4e5c', width=2)
    draw.text((60, 53), '全成就达成', font=_font(52, bold=True), fill='#f1f5f9')
    draw.line((60, 148, 1140, 148), fill='#2c3c49', width=2)
    from pathlib import Path
    with Image.open(Path(__file__).resolve().parent.parent / 'assets' / 'steam-perfect.png') as badge:
        badge = badge.convert('RGBA').resize((104, 104), Image.Resampling.LANCZOS)
        canvas.paste(badge, (1028, 28), badge)
    _rounded_paste(canvas, _card_image(avatar, (70, 70), '#314655'), (60, 174), 35)
    text, font = _fitted_text(draw, name, 620, 32, 24)
    draw.text((148, 187), text, font=font, fill='#e1e8f0')
    draw.rounded_rectangle((816, 187, 1138, 237), radius=17, fill='#263b48', outline='#537386')
    label = f'已解锁 {total}/{total} · 100%'
    text, font = _fitted_text(draw, label, 294, 24, 18)
    draw.text((977, 212), text, font=font, fill='#b9e3f8', anchor='mm')
    draw.rounded_rectangle((60, 262, 500, 704), radius=25, fill='#1b2833', outline='#657989', width=2)
    _rounded_paste(canvas, _card_image(cover, (410, 412), '#293e4c'), (75, 277), 18)
    if cover is None:
        draw.text((280, 480), 'STEAM', font=_font(42, bold=True), fill='#7e9db1', anchor='mm')
    # Character wrapping handles both CJK and unbroken long titles.
    font = _font(42, bold=True)
    lines, line = [], ''
    for char in title:
        if draw.textlength(line + char, font=font) > 575 and line:
            lines.append(line)
            line = char
        else:
            line += char
    if line:
        lines.append(line)
    if len(lines) > 3:
        lines = lines[:3]
        lines[-1], _ = _fitted_text(draw, lines[-1] + '…', 575, 42, 42)
    for i, line in enumerate(lines):
        draw.text((550, 302 + i * 60), line, font=font, fill='#f2f6fa')
    draw.line((550, 527, 1138, 527), fill='#293d4c', width=2)
    duration = '暂无数据' if minutes is None else f'{minutes // 60}小时{minutes % 60}分钟'
    text, font = _fitted_text(draw, '累计游玩：' + duration, 580, 29, 21)
    draw.text((550, 560), text, font=font, fill='#96c9e9')
    stamp = datetime.fromtimestamp(achieved_at or detected_at, timezone(timedelta(hours=8)))
    draw.text((1138, 757), ('达成于 ' if achieved_at else '检测于 ') + stamp.strftime('%Y/%m/%d %H:%M'),
              font=_font(23), fill='#92a8b9', anchor='rs')
    output = BytesIO()
    canvas.save(output, format='PNG')
    return output.getvalue()


async def build_message(name, payload, avatar_url=None):
    message = Message(MessageSegment.text(f'{name}在《{payload["title"]}》中获得全成就。'))
    try:
        async with asyncio.timeout(18):
            avatar, cover = await asyncio.gather(_download_card_image(avatar_url, 'avatar'),
                                                 _download_card_image(payload.get('cover'), 'cover'))
            card = await asyncio.to_thread(render_card, name, payload['title'], payload['total'],
                                           payload.get('minutes'), payload.get('achieved_at'),
                                           payload['detected_at'], avatar, cover)
        message += MessageSegment.image(card, cache=False, proxy=False, timeout=30)
    except Exception:
        logger.warning('Steam achievement card unavailable; sending text', exc_info=True)
    return message

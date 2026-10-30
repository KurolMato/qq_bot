"""Render offline QA samples with the production Steam achievement renderer."""
from pathlib import Path
import sys
from datetime import datetime, timezone
from io import BytesIO

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from qq_bot.steam_achievement_card import render_card


def main():
    output = ROOT / 'assets' / 'previews' / 'steam-achievement-production-20261006'
    output.mkdir(parents=True, exist_ok=True)
    cover_path = ROOT / 'assets/previews/rank-me-assets/persona.png'
    cover = cover_path.read_bytes() if cover_path.is_file() else None
    icon = Image.new('RGB', (96,96), '#24364b')
    draw = ImageDraw.Draw(icon)
    draw.regular_polygon((48,48,32), n_sides=5, fill='#efdba8')
    encoded = BytesIO()
    icon.save(encoded, format='PNG')
    stamp = datetime(2026,10,4,15,18,tzinfo=timezone.utc).timestamp()
    samples = [
        ('reference', '示例玩家', '女神异闻录５ 皇家版', 5184, cover, {
            'first_achieved_at': stamp - 43*86400,
            'achievement_name': '最后的拼图',
            'achievement_description': '完成最后一项挑战，为旅程画上句号。',
            'achievement_percent': 3.8, 'achievement_icon': encoded.getvalue(),
        }),
        ('chinese', '示例昵称'*8, '这是用于检查排版的超长中文游戏名称：完全版与额外内容合集'*3,
         None, None, {'achievement_name': '这是一条非常长的成就名称'*8,
                      'achievement_description': '这是一条很长的成就描述，用于检查自动换行与截断。'*10}),
        ('english', 'A very long Steam display name for layout verification',
         'MONSTER HUNTER RISE SUNBREAK DELUXE COMPLETE EDITION EXTREMELY LONG TITLE',
         9999999, None, {}),
    ]
    for filename, name, title, minutes, art, details in samples:
        content = render_card(name,title,52,minutes,stamp if filename == 'reference' else None,
                              stamp,cover=art,**details)
        path = output / (filename + '.jpg')
        path.write_bytes(content)
        print(f'{path}: {len(content)} bytes')


if __name__ == '__main__':
    main()

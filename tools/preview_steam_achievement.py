"""Generate the bundled 100% ribbon and local visual QA cards (no network)."""
from pathlib import Path
import sys
import time
from io import BytesIO
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from qq_bot.switch_presence import _font
from qq_bot.steam_achievement_card import render_card


def main():
    # Locally drawn Steam-style perfect-game ribbon, not a downloaded Valve logo.
    badge = Image.new('RGBA', (256, 256))
    d = ImageDraw.Draw(badge)
    d.polygon([(63, 135), (122, 151), (98, 251), (70, 222), (36, 230)], fill='#3f88ce')
    d.polygon([(130, 151), (191, 135), (220, 230), (186, 222), (159, 251)], fill='#3977b4')
    import math
    points = [(128 + (111 if i % 2 == 0 else 97)*math.cos(i*math.pi/16),
               115 + (111 if i % 2 == 0 else 97)*math.sin(i*math.pi/16)) for i in range(32)]
    d.polygon(points, fill='#659ac5')
    d.ellipse((42, 29, 214, 201), fill='#204967', outline='#b5d8e8', width=8)
    d.text((128, 113), '100%', font=_font(43, bold=True), fill='#ffffff', anchor='mm')
    d.text((128, 156), 'STEAM', font=_font(22, bold=True), fill='#bce5f8', anchor='mm')
    badge.save(ROOT / 'assets' / 'steam-perfect.png')
    output = ROOT / 'data' / 'achievement-preview'
    output.mkdir(parents=True, exist_ok=True)
    cover = Image.new('RGB', (600, 900), '#2a4b62')
    cd = ImageDraw.Draw(cover)
    cd.text((300, 370), 'MONSTER\nHUNTER\nRISE', font=_font(56, bold=True), fill='#d7e9f4', anchor='mm', align='center')
    buf = BytesIO()
    cover.save(buf, format='PNG')
    samples = [('reference', 'ExamplePSN', 'MONSTER HUNTER RISE', 123*60+45, buf.getvalue()),
               ('chinese', '一缕红唇守青灯', '这是用于检查排版的超长中文游戏名称：完全版与额外内容合集', None, None),
               ('english', 'A very long Steam display name for layout verification',
                'MONSTER HUNTER RISE SUNBREAK DELUXE COMPLETE EDITION EXTREMELY LONG TITLE', 999999, None)]
    for filename, name, title, minutes, art in samples:
        content = render_card(name, title, 50, minutes, None, time.time(), cover=art)
        (output / (filename + '.png')).write_bytes(content)
    print(output)


if __name__ == '__main__':
    main()

"""Offline previews of the shared daily/weekly/monthly glass leaderboard."""
from datetime import datetime
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from qq_bot.game_time_tracker import LOCAL_TZ, LeaderboardSnapshot, TrackedMember, TrackedGame
from qq_bot.game_time_leaderboard import _render


def main():
    art = ROOT / 'assets/previews/rank-me-assets'
    assets = {p.stem: p.read_bytes() for p in art.glob('*.png')}
    def game(platform, name, minutes, image):
        return TrackedGame(platform, name, minutes * 60, image)
    groups = [
        ('星野', 'avatar', (game('switch', '塞尔达传说：旷野之息', 260, 'zelda'),
                         game('ps5', '女神异闻录 5 皇家版', 168, 'persona'),
                         game('steam', '红色沙漠', 94, 'crimson'))),
        ('Mato', None, (game('steam', 'Grand Theft Auto V', 250, 'gta'), game('ps5', '红色沙漠', 125, 'crimson'))),
        ('雨落', None, (game('switch', '火焰纹章 风花雪月', 276, 'fire-emblem'),)),
        ('北川', None, (game('steam', '幸存者笔记', 138, 'survivor'),)),
        ('小满', None, (game('xbox', 'Grand Theft Auto V', 66, 'gta'),)),
    ]
    members = tuple(TrackedMember(name, avatar, sum(g.seconds for g in games), games) for name, avatar, games in groups)
    output = ROOT / 'assets/previews/rank-board-glass'
    output.mkdir(parents=True, exist_ok=True)
    for period in ('day', 'week', 'month'):
        start = datetime(2026, 11, {'day': 7, 'week': 2, 'month': 1}[period], 4, tzinfo=LOCAL_TZ)
        snapshot = LeaderboardSnapshot(start, datetime(2026, 11, 7, 21, 30, tzinfo=LOCAL_TZ), members, period)
        path = output / f'{period}.png'
        path.write_bytes(_render(snapshot, assets))
        print(path)


if __name__ == '__main__':
    main()

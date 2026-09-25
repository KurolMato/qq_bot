"""Read-only, group-scoped Steam achievement lookup and progress cards."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from io import BytesIO

from PIL import Image, ImageDraw
from nonebot.adapters.onebot.v11 import Message, MessageSegment

from .game_calendar import GameLookupError, STORE_SEARCH_URL, _match_score
from .steam_achievements import parse_progress, _playtime_minutes
from .switch_presence import _card_image, _fitted_text, _font, _rounded_paste
from .list_cards import _load_image

CHINA = timezone(timedelta(hours=8))


@dataclass
class PlayerProgress:
    name: str
    account: str
    avatar: str | None
    unlocked: int
    total: int
    completed_at: int | None
    minutes: int | None

    @property
    def complete(self):
        return self.total > 0 and self.unlocked == self.total


@dataclass
class GameProgress:
    app_id: str
    title: str
    cover: str | None
    players: list[PlayerProgress]
    unavailable: int = 0
    not_owned: int = 0


class ProgressService:
    def __init__(self, client, registry):
        self.client, self.registry = client, registry
        self.slots = asyncio.Semaphore(4)

    async def resolve(self, query):
        query = query.strip()
        if query.isdigit():
            app_id = query
        else:
            data = await self.client._public_get(STORE_SEARCH_URL, term=query, l='schinese', cc='cn')
            candidates = [v for v in data.get('items', [])
                          if isinstance(v, dict) and v.get('type') == 'app' and str(v.get('id', '')).isdigit()]
            if not candidates:
                raise GameLookupError('没有找到游戏，请输入更完整的名称或 Steam AppID。')
            ranked = sorted(enumerate(candidates), key=lambda v: _match_score(query, v[1].get('name', ''), v[0]), reverse=True)
            best = ranked[0][1]
            # Do not silently choose between close matches such as different editions.
            if len(ranked) > 1 and _match_score(query, best.get('name', ''), 0) < 190:
                options = '；'.join(f'{v.get("name")}（{v["id"]}）' for _, v in ranked[:5])
                raise GameLookupError('找到多个游戏，请用 /game AppID 精确查询：' + options)
            app_id = str(best['id'])
        details = await self.client.game_details(app_id)
        if not details or details.get('type') != 'game':
            raise GameLookupError('没有找到该 Steam 游戏，请检查名称或 AppID。')
        return app_id, str(details.get('name') or app_id), details.get('header_image')

    async def player(self, item, app_id, schema):
        async with self.slots:
            minutes = None
            absent = False
            try:
                async with asyncio.timeout(12):
                    owned = await self.client._get('IPlayerService/GetOwnedGames/v1/', steamid=item.steam_id,
                                                  appids_filter=[int(app_id)], include_played_free_games=True)
                    minutes = _playtime_minutes(owned, app_id)
                    response = owned.get('response', {})
                    absent = response.get('game_count') == 0 or response.get('games') == []
            except Exception:
                pass  # Achievement visibility and playtime visibility are independent.
            try:
                async with asyncio.timeout(18):
                    raw = await self.client._get('ISteamUserStats/GetPlayerAchievements/v1/',
                                                 steamid=item.steam_id, appid=app_id, l='english',
                                                 _quiet_statuses={400, 403})
                progress = parse_progress(raw)
                if set(progress) != schema:
                    return 'unavailable'
                unlocked = sum(v[0] for v in progress.values())
                # An empty library alone is not proof of non-ownership (private profiles).
                if absent and not unlocked and minutes is None:
                    return 'not_owned'
                stamps = [v[1] for v in progress.values()]
                valid = all(type(t) is int and 0 < t <= time.time() for t in stamps)
                completed_at = max(stamps) if unlocked == len(schema) and valid else None
                return PlayerProgress(item.nickname or item.display_name, item.display_name,
                                      item.avatar_url, unlocked, len(schema), completed_at, minutes)
            except Exception:
                return 'unavailable'

    async def query(self, group_id, query):
        items = await asyncio.to_thread(self.registry.list_group, group_id)
        if not items:
            raise GameLookupError('本群还没有登记 Steam 账号，请先使用 /steam add 登记。')
        if not self.client.configured:
            raise GameLookupError('Steam API Key 尚未配置，请先配置 Steam 服务。')
        async with asyncio.timeout(90):
            app_id, title, cover = await self.resolve(query)
            raw = await self.client._get('ISteamUserStats/GetSchemaForGame/v2/', appid=app_id, l='english')
            values = raw.get('game', {}).get('availableGameStats', {}).get('achievements')
            if not values:
                raise GameLookupError(f'《{title}》没有可查询的 Steam 成就。')
            schema = {v['name'] for v in values if isinstance(v, dict) and v.get('name')}
            if len(schema) != len(values):
                raise GameLookupError('Steam 成就定义暂不完整，请稍后重试。')
            results = await asyncio.gather(*(self.player(v, app_id, schema) for v in items))
        players = [v for v in results if isinstance(v, PlayerProgress)]
        players.sort(key=lambda v: (not v.complete, (v.completed_at or float('inf')) if v.complete else -v.unlocked,
                                    v.name.casefold()))
        return GameProgress(app_id, title, cover, players, results.count('unavailable'), results.count('not_owned'))


def render_progress(result, rows, assets, page=1, pages=1):
    sections = [(label, [v for v in rows if v.complete == complete])
                for label, complete in [('已全成就', True), ('未全成就', False)]]
    height = 405 + sum(72 + max(1, len(group)) * 132 for _, group in sections) + 100
    canvas = Image.new('RGB', (1200, height), '#0d161e')
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle((1, 1, 1198, height-2), 30, outline='#3b4e5d', width=2)
    draw.line((64, 44, 96, 44), fill='#cbb682', width=3)
    draw.text((64, 65), '游戏进度', font=_font(54, bold=True), fill='#edf2f7')
    draw.text((1135, 110), f'STEAM · {page}/{pages}', font=_font(22), fill='#8da3b5', anchor='ra')
    draw.line((64, 167, 1135, 167), fill='#344450', width=2)
    draw.rounded_rectangle((48, 194, 1152, 366), 24, fill='#1b2833')
    _rounded_paste(canvas, _card_image(assets.get(result.cover), (132, 132), '#304653'), (72, 214), 15)
    title, font = _fitted_text(draw, result.title, 882, 38, 24)
    draw.text((238, 239), title, font=font, fill='#e3ebf2')
    draw.text((240, 301), f'AppID {result.app_id} · 本群 Steam 登记账号', font=_font(22), fill='#8da3b5')
    y = 399
    for label, group in sections:
        complete = label == '已全成就'
        count = sum(v.complete == complete for v in result.players)
        draw.text((64, y), label, font=_font(30, bold=True), fill='#e4ebf2')
        draw.text((1135, y), str(count), font=_font(30), fill='#cbb682', anchor='ra')
        y += 60
        if not group:
            draw.text((72, y+32), '本页暂无记录' if pages > 1 else '暂无记录', font=_font(26), fill='#8da3b5')
            y += 132
        for player in group:
            rank = [v for v in result.players if v.complete == complete].index(player) + 1
            draw.rounded_rectangle((48, y, 1152, y+118), 24, fill='#192630', outline='#34434e')
            draw.text((84, y+32), f'{rank:02}', font=_font(38), fill='#cbb682' if rank <= 3 else '#91a6b9')
            _rounded_paste(canvas, _card_image(assets.get(player.avatar), (80, 80), '#314655'), (192, y+19), 40)
            name, font = _fitted_text(draw, player.name, 405, 32, 22)
            draw.text((304, y+13), name, font=font, fill='#e4ebf2')
            account, font = _fitted_text(draw, player.account, 405, 21, 18)
            draw.text((305, y+56), account, font=font, fill='#92a5b7')
            draw.text((305, y+87), f'STEAM · {player.unlocked}/{player.total}', font=_font(18), fill='#92a5b7')
            if complete:
                stamp = datetime.fromtimestamp(player.completed_at, CHINA).strftime('%Y.%m.%d %H:%M') if player.completed_at else '达成时间未记录'
                draw.text((1115, y+21), stamp, font=_font(25), fill='#c4cfda', anchor='ra')
            else:
                pct = player.unlocked / player.total
                draw.text((1115, y+8), f'{int(pct*100)}%', font=_font(30), fill='#d3bf91', anchor='ra')
                draw.rounded_rectangle((750, y+54, 1115, y+59), 2, fill='#3b505e')
                if pct:
                    draw.rectangle((750, y+54, 750+365*pct, y+59), fill='#cbb682')
            duration = '累计时长未知' if player.minutes is None else f'累计 {player.minutes//60} 小时 {player.minutes%60} 分钟'
            draw.text((1115, y+78), duration, font=_font(21), fill='#92a5b7', anchor='ra')
            y += 132
        y += 12
    draw.text((64, height-76), f'无法查询 {result.unavailable} 人（隐私 / 接口异常） · 无游玩记录 {result.not_owned} 人', font=_font(21), fill='#92a5b7')
    draw.text((1135, height-40), '累计时长非全成就耗时 · ' + datetime.now(CHINA).strftime('%Y.%m.%d %H:%M'), font=_font(18), fill='#758b9e', anchor='ra')
    output = BytesIO()
    canvas.save(output, 'PNG')
    return output.getvalue()


async def build_progress_messages(result):
    chunks = [result.players[i:i+12] for i in range(0, len(result.players), 12)] or [[]]
    # Share the list renderer's persistent avatar/cover cache. Reuse each URL
    # across pages too, including failed loads, instead of downloading it again.
    assets = {}
    semaphore = asyncio.Semaphore(6)
    for page, rows in enumerate(chunks, 1):
        urls = list(dict.fromkeys(v for v in [result.cover, *(p.avatar for p in rows)]
                                 if v and v not in assets))
        assets.update(zip(urls, await asyncio.gather(*(_load_image(url, semaphore) for url in urls))))
        card = await asyncio.to_thread(render_progress, result, rows, assets, page, len(chunks))
        yield Message(MessageSegment.image(card, cache=False, proxy=False, timeout=30))

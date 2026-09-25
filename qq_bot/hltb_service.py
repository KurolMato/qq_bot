"""Optional HLTB enrichment, isolated from presence and video workers."""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import sqlite3
import time
import threading
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import datetime
from contextlib import contextmanager
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)
UNAVAILABLE = "通关时长查询暂时不可用，请稍后再试。"


class HltbError(RuntimeError):
    pass


def clean_name(value: str) -> str:
    value = value.strip()
    if value.startswith("《") and value.endswith("》"):
        value = value[1:-1].strip()
    if not value or len(value) > 200:
        raise HltbError("用法：/hltb 游戏名，例如 /hltb 《艾尔登法环》")
    return value


def name_key(value: str) -> str:
    # Keep punctuation and edition/sequence words: they can distinguish games.
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


@dataclass(frozen=True)
class Game:
    id: str
    name: str
    main: float | None = None
    extra: float | None = None
    complete: float | None = None
    updated: float = 0


@dataclass(frozen=True)
class Choice:
    id: str
    name: str
    kind: str = "hltb"
    game: Game | None = None


@dataclass
class Result:
    game: Game | None = None
    choices: list[Choice] = field(default_factory=list)
    stale: bool = False
    message: str = ""


class Cache:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()

    @contextmanager
    def connect(self):
        # Operations run in worker threads; serialise the tiny transactions,
        # including first-use WAL/schema setup, without blocking the event loop.
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(self.path, timeout=2)
            try:
                db.execute("PRAGMA journal_mode=WAL")
                db.executescript("""
                CREATE TABLE IF NOT EXISTS games (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS aliases (query TEXT PRIMARY KEY, game_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS misses (query TEXT PRIMARY KEY, expires REAL NOT NULL);
                """)
                with db:
                    yield db
            finally:
                db.close()

    def read(self, query: str) -> tuple[Game | None, bool]:
        with self.connect() as db:
            row = db.execute("SELECT g.payload FROM aliases a JOIN games g ON a.game_id=g.id WHERE a.query=?", (query,)).fetchone()
            miss = db.execute("SELECT expires FROM misses WHERE query=?", (query,)).fetchone()
        return (Game(**json.loads(row[0])) if row else None, bool(miss and miss[0] > time.time()))

    def save(self, query: str, game: Game):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO games VALUES (?,?)", (game.id, json.dumps(asdict(game))))
            db.execute("INSERT OR REPLACE INTO aliases VALUES (?,?)", (query, game.id))
            db.execute("DELETE FROM misses WHERE query=?", (query,))

    def miss(self, query: str):
        with self.connect() as db:
            db.execute("DELETE FROM misses WHERE expires<?", (time.time(),))
            db.execute("INSERT OR REPLACE INTO misses VALUES (?,?)", (query, time.time() + 600))


class Provider:
    @staticmethod
    def client():
        from howlongtobeatpy import HowLongToBeat

        class BotHowLongToBeat(HowLongToBeat):
            def __init__(self):
                # Upstream 1.0.23 sets WindowsSelectorEventLoopPolicy in __init__.
                # Only initialise parser options, preserving the host's loop policy.
                self.minimum_similarity = 0.0
                self.auto_filter_times = False

        return BotHowLongToBeat()

    @staticmethod
    def convert(entry) -> Game:
        def hours(value):
            try:
                number = float(value)
                return number if math.isfinite(number) and number > 0 else None
            except (ValueError, TypeError):
                return None
        if not str(entry.game_id).isdigit() or not entry.game_name:
            raise HltbError(UNAVAILABLE)
        return Game(str(entry.game_id), str(entry.game_name), hours(entry.main_story),
                    hours(entry.main_extra), hours(entry.completionist), time.time())

    async def search(self, query: str) -> list[Game]:
        values = await self.client().async_search(query, similarity_case_sensitive=False)
        if values is None:
            raise HltbError(UNAVAILABLE)
        return list({str(v.game_id): self.convert(v) for v in values}.values())

    async def detail(self, game_id: str) -> Game:
        value = await self.client().async_search_from_id(int(game_id))
        if value is None or str(value.game_id) != game_id:
            raise HltbError(UNAVAILABLE)
        return self.convert(value)

    async def steam_search(self, query: str) -> list[Choice]:
        async with httpx.AsyncClient(timeout=8, follow_redirects=True) as client:
            response = await client.get("https://store.steampowered.com/api/storesearch/",
                                        params={"term": query, "l": "schinese", "cc": "cn"})
            response.raise_for_status()
            items = response.json().get("items")
        if not isinstance(items, list):
            raise HltbError(UNAVAILABLE)
        return list({str(v["id"]): Choice(str(v["id"]), v["name"], "steam")
                     for v in items if v.get("type") == "app" and str(v.get("id", "")).isdigit() and v.get("name")}.values())

    async def english_name(self, app_id: str) -> str:
        async with httpx.AsyncClient(timeout=8, follow_redirects=True) as client:
            response = await client.get("https://store.steampowered.com/api/appdetails",
                                        params={"appids": app_id, "l": "english", "cc": "us"})
            response.raise_for_status()
            item = response.json().get(app_id, {})
        name = item.get("data", {}).get("name")
        if not item.get("success") or not name:
            raise HltbError("无法获取这个游戏的英文名，请直接输入英文全名。")
        return str(name)


class HltbService:
    def __init__(self, path: Path, provider=None, *, timeout=20.0, ttl=7 * 86400):
        self.cache = Cache(path)
        self.provider = provider or Provider()
        self.timeout = timeout
        self.ttl = ttl
        self.gate = asyncio.Semaphore(2)
        self.jobs: dict[str, asyncio.Task] = {}
        self.cooldown: dict[str, float] = {}
        self.pending: dict[tuple[str, str], tuple[float, str, list[Choice]]] = {}

    async def _execute(self, key, operation, deadline):
        try:
            async with asyncio.timeout_at(deadline):
                async with self.gate:
                    return await operation()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.cooldown[key] = time.monotonic() + 30
            logger.warning("HLTB request failed (%s)", key, exc_info=True)
            if isinstance(exc, HltbError):
                raise
            raise HltbError(UNAVAILABLE) from exc

    def _job(self, key, operation, deadline=None):
        self.cooldown = {k: t for k, t in self.cooldown.items() if t > time.monotonic()}
        if key in self.cooldown:
            raise HltbError(UNAVAILABLE)
        if key not in self.jobs:
            if len(self.jobs) >= 64:
                raise HltbError(UNAVAILABLE)
            deadline = deadline if deadline is not None else asyncio.get_running_loop().time() + self.timeout
            task = asyncio.create_task(self._execute(key, operation, deadline))
            self.jobs[key] = task

            def done(finished):
                self.jobs.pop(key, None)
                if not finished.cancelled():
                    finished.exception()  # Background refresh failures are already logged.
            task.add_done_callback(done)
        return self.jobs[key]

    async def _refresh(self, query, game):
        updated = await self.provider.detail(game.id)
        await asyncio.to_thread(self.cache.save, query, updated)
        return Result(game=updated)

    async def _match_hltb(self, original: str, search: str) -> Result:
        games = await self.provider.search(search)
        matches = [g for g in games if name_key(g.name) == name_key(search)]
        if len(matches) == 1:
            await asyncio.to_thread(self.cache.save, original, matches[0])
            return Result(game=matches[0])
        if games:
            return Result(choices=[Choice(g.id, g.name, game=g) for g in games[:5]])
        await asyncio.to_thread(self.cache.miss, original)
        return Result(message="没有找到通关时长数据，请尝试更完整的英文游戏名。")

    async def _search(self, key: str, query: str) -> Result:
        if re.search(r"[\u3400-\u9fff]", query):
            choices = await self.provider.steam_search(query)
            exact = [c for c in choices if name_key(c.name) == name_key(query)]
            if len(exact) == 1:
                english = await self.provider.english_name(exact[0].id)
                return await self._match_hltb(key, english)
            if choices:
                return Result(choices=choices[:5])
            await asyncio.to_thread(self.cache.miss, key)
            return Result(message="未匹配到中文游戏名，请尝试英文全名。")
        return await self._match_hltb(key, query)

    def _remember_choices(self, owner, key, result):
        self.pending = {k: v for k, v in self.pending.items() if v[0] > time.monotonic()}
        self.pending.pop(owner, None)
        if result.choices:
            if len(self.pending) >= 256:
                self.pending.pop(next(iter(self.pending)))
            self.pending[owner] = (time.monotonic() + 300, key, result.choices)
        return result

    async def query(self, value: str, owner: tuple[str, str]) -> Result:
        query = clean_name(value)
        key = name_key(query)
        deadline = asyncio.get_running_loop().time() + self.timeout
        try:
            async with asyncio.timeout_at(deadline):
                game, missed = await asyncio.to_thread(self.cache.read, key)
                if game:
                    stale = time.time() - game.updated >= self.ttl
                    if stale:
                        try:
                            self._job("refresh:" + game.id, lambda: self._refresh(key, game))
                        except HltbError:
                            pass
                    return self._remember_choices(owner, key, Result(game=game, stale=stale))
                if missed:
                    return self._remember_choices(owner, key, Result(message="没有找到通关时长数据，请尝试完整的英文游戏名。"))
                result = await asyncio.shield(self._job("search:" + key, lambda: self._search(key, query), deadline))
                return self._remember_choices(owner, key, result)
        except TimeoutError as exc:
            raise HltbError(UNAVAILABLE) from exc

    async def select(self, number: int, owner: tuple[str, str]) -> Result:
        pending = self.pending.get(owner)
        if pending is None or pending[0] <= time.monotonic():
            self.pending.pop(owner, None)
            raise HltbError("候选已过期或不存在，请重新发送 /hltb 游戏名。")
        _, key, choices = pending
        if not 1 <= number <= len(choices):
            raise HltbError(f"请输入 1 至 {len(choices)} 的候选序号。")
        choice = choices[number - 1]

        async def resolve():
            if choice.kind == "steam":
                english = await self.provider.english_name(choice.id)
                return await self._match_hltb(key, english)
            assert choice.game is not None
            await asyncio.to_thread(self.cache.save, key, choice.game)
            return Result(game=choice.game)

        result = await asyncio.shield(self._job(f"select:{key}:{choice.kind}:{choice.id}", resolve))
        return self._remember_choices(owner, key, result)

    async def close(self):
        tasks = list(self.jobs.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.jobs.clear()
        self.pending.clear()


def format_result(result: Result) -> str:
    if result.game:
        game = result.game
        def hours(value):
            return f"约 {value:g} 小时" if value and value > 0 else "暂无数据"
        text = (f"{game.name}\n主线：{hours(game.main)}\n主线＋支线：{hours(game.extra)}\n"
                f"全收集：{hours(game.complete)}\n来源：HowLongToBeat\nhttps://howlongtobeat.com/game/{game.id}")
        if result.stale:
            text += "\n缓存更新于：" + datetime.fromtimestamp(game.updated).strftime("%Y-%m-%d %H:%M")
        return text
    if result.choices:
        lines = ["请选择游戏："]
        lines.extend(f"{i}. {c.name}（{c.kind.upper()} ID: {c.id}）" for i, c in enumerate(result.choices, 1))
        lines.append("发送 /hltb select 序号（5分钟内有效）")
        return "\n".join(lines)
    return result.message

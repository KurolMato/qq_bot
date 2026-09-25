import asyncio
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from qq_bot.hltb_service import (
    Cache, Choice, Game, HltbError, HltbService, Provider, Result,
    clean_name, format_result, name_key,
)


class FakeProvider:
    def __init__(self):
        self.games = [Game("1", "Elden Ring", 60, 100, 140, time.time())]
        self.steam = [Choice("1245620", "艾尔登法环", "steam")]
        self.calls = 0
        self.delay = 0
        self.failed = False
        self.active = 0
        self.peak = 0
        self.started = asyncio.Event()

    async def search(self, query):
        self.calls += 1
        self.active += 1
        self.started.set()
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(self.delay)
            if self.failed:
                raise RuntimeError("network error")
            return self.games
        finally:
            self.active -= 1

    async def steam_search(self, query):
        return self.steam

    async def english_name(self, appid):
        assert appid == "1245620"
        return "Elden Ring"

    async def detail(self, gameid):
        return (await self.search(""))[0]


@pytest.mark.parametrize("value", [" Elden Ring ", "《Elden Ring》"])
def test_clean_name(value):
    assert clean_name(value) == "Elden Ring"
    assert name_key("ELDEN  RING") == "elden ring"
    assert name_key("Game 2") != name_key("Game")
    assert name_key("Game Remastered") != name_key("Game")


def test_zero_missing_and_source():
    text = format_result(Result(game=Game("12", "Game", 0, None, 12.5)))
    assert text.count("暂无数据") == 2
    assert "约 12.5 小时" in text
    assert "https://howlongtobeat.com/game/12" in text


def test_cache_survives_restart_and_chinese_mapping(tmp_path):
    async def run():
        path = tmp_path / "cache.db"
        p = FakeProvider()
        s = HltbService(path, p)
        result = await s.query("《艾尔登法环》", ("g", "u"))
        assert result.game.id == "1"
        p.failed = True
        restarted = HltbService(path, p)
        assert (await restarted.query("艾尔登法环", ("g2", "u2"))).game.id == "1"
        assert p.calls == 1
    asyncio.run(run())


def test_candidates_are_isolated_and_expire(tmp_path):
    async def run():
        p = FakeProvider()
        p.games = [Game(str(i), f"Game {i}") for i in range(1, 8)]
        s = HltbService(tmp_path / "cache.db", p)
        result = await s.query("Game", ("a", "b"))
        assert len(result.choices) == 5 and result.game is None
        for owner in [("other", "b"), ("a", "other")]:
            with pytest.raises(HltbError):
                await s.select(1, owner)
        with pytest.raises(HltbError):
            await s.select(6, ("a", "b"))
        assert (await s.select(2, ("a", "b"))).game.name == "Game 2"
        assert (await s.query("Game", ("a", "b"))).game.name == "Game 2"
        await s.query("Other", ("a", "b"))
        _, key, choices = s.pending[("a", "b")]
        s.pending[("a", "b")] = (0, key, choices)
        with pytest.raises(HltbError):
            await s.select(1, ("a", "b"))
    asyncio.run(run())


def test_steam_candidate_selection_then_hltb_selection(tmp_path):
    async def run():
        p = FakeProvider()
        p.steam = [Choice("1245620", "艾尔登法环 豪华版", "steam")]
        p.games = [Game("1", "Elden Ring DLC", updated=time.time())]
        s = HltbService(tmp_path / "cache.db", p)
        assert (await s.query("艾尔登", ("g", "u"))).choices[0].kind == "steam"
        assert (await s.select(1, ("g", "u"))).choices[0].kind == "hltb"
        assert (await s.select(1, ("g", "u"))).game.name == "Elden Ring DLC"
        assert (await s.query("艾尔登", ("g2", "u2"))).game.id == "1"
    asyncio.run(run())


def test_miss_cached_but_failure_not_cached(tmp_path):
    async def run():
        p = FakeProvider()
        p.games = []
        s = HltbService(tmp_path / "cache.db", p)
        assert (await s.query("Unknown", ("g", "u"))).message
        await s.query("Unknown", ("g", "u"))
        assert p.calls == 1
        p.failed = True
        with pytest.raises(HltbError):
            await s.query("Network", ("g", "u"))
        assert s.cache.read("network") == (None, False)
        with pytest.raises(HltbError):
            await s.query("Network", ("g", "u"))
        assert p.calls == 2
    asyncio.run(run())


def test_stale_return_refresh_and_failed_refresh_keeps_cache(tmp_path):
    async def run():
        p = FakeProvider()
        s = HltbService(tmp_path / "cache.db", p)
        old = replace(p.games[0], updated=1)
        s.cache.save("elden ring", old)
        p.delay = .03
        p.failed = True
        result = await s.query("Elden Ring", ("g", "u"))
        assert result.stale and result.game.updated == 1
        assert "缓存更新于" in format_result(result)
        await asyncio.gather(*list(s.jobs.values()), return_exceptions=True)
        assert s.cache.read("elden ring")[0].updated == 1
        s.cooldown.clear()
        p.failed = False
        await s.query("Elden Ring", ("g", "u"))
        await asyncio.gather(*list(s.jobs.values()))
        assert s.cache.read("elden ring")[0].updated > 1
    asyncio.run(run())


def test_singleflight_and_event_loop_remains_responsive(tmp_path):
    async def run():
        p = FakeProvider()
        p.delay = .05
        s = HltbService(tmp_path / "cache.db", p)
        ticks = []
        async def other_command():
            await p.started.wait()
            await asyncio.sleep(.01)
            ticks.append(p.active)
        results = await asyncio.gather(s.query("Elden Ring", ("1", "1")),
                                       s.query("Elden Ring", ("2", "2")), other_command())
        assert results[0].game == results[1].game
        assert p.calls == 1 and ticks == [1]
    asyncio.run(run())


def test_queue_deadline_releases_slots_and_shutdown(tmp_path):
    async def run():
        p = FakeProvider()
        p.delay = 5
        s = HltbService(tmp_path / "cache.db", p, timeout=.05)
        start = time.monotonic()
        results = await asyncio.gather(*(s.query(f"Game {i}", ("g", str(i))) for i in range(5)), return_exceptions=True)
        assert all(isinstance(r, HltbError) for r in results)
        assert time.monotonic() - start < .5
        await asyncio.sleep(.02)
        assert p.active == 0 and p.peak <= 2 and not s.jobs
        s.timeout = 10
        p.started.clear()
        task = asyncio.create_task(s.query("New", ("g", "u")))
        await p.started.wait()
        await s.close()
        await asyncio.gather(task, return_exceptions=True)
        assert p.active == 0 and not s.jobs
    asyncio.run(run())


def test_provider_preserves_event_loop_policy_and_handles_none(monkeypatch):
    policy = asyncio.get_event_loop_policy()
    client = Provider.client()
    assert asyncio.get_event_loop_policy() is policy
    assert client.minimum_similarity == 0
    async def search(*a, **kw):
        return None
    monkeypatch.setattr(Provider, "client", staticmethod(lambda: SimpleNamespace(async_search=search)))
    with pytest.raises(HltbError):
        asyncio.run(Provider().search("Game"))


def test_steam_english_name_without_release_date(monkeypatch):
    import httpx
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"1245620": {"success": True, "data": {"name": "Elden Ring"}}})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(respond), **kw))
    assert asyncio.run(Provider().english_name("1245620")) == "Elden Ring"
    assert requests[0].url.params["l"] == "english"


def test_command_quotes_once_and_obeys_whitelist(monkeypatch, tmp_path):
    import nonebot
    nonebot.init()
    from nonebot.adapters.onebot.v11 import Message
    from qq_bot import hltb_commands as commands
    s = HltbService(tmp_path / "cache.db", FakeProvider())
    monkeypatch.setattr(commands, "service", s)
    monkeypatch.setattr(commands, "config", SimpleNamespace(allowed_groups={"100"}))
    sent = []
    async def send(event, message):
        sent.append(message)
    async def run():
        event = SimpleNamespace(group_id=99, user_id=5, message_id=123)
        bot = SimpleNamespace(send=send)
        await commands.handle_hltb(bot, event, Message("Elden Ring"))
        assert sent == []
        event.group_id = 100
        await commands.handle_hltb(bot, event, Message("Elden Ring"))
        assert len(sent) == 1
        assert sent[0][0].type == "reply" and str(sent[0][0].data["id"]) == "123"
        assert "主线：" in sent[0].extract_plain_text()
        await commands.handle_hltb(bot, event, Message(""))
        assert "用法：" in sent[-1].extract_plain_text()
        await s.close()
    asyncio.run(run())

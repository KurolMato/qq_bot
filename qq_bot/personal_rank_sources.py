"""Native store artwork for rank cards; never queries player achievements."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
import time
import unicodedata
from urllib.parse import quote
from pathlib import Path

_cache: dict[tuple, tuple[float, tuple[str, ...]]] = {}
logger = logging.getLogger(__name__)


def canonical_cover_title(value: str) -> str:
    """Unify verified regional spellings, keeping sequel and edition suffixes."""
    # NFKC expands ™ into literal TM, which breaks store searches and matching.
    value = unicodedata.normalize("NFKC", re.sub(r"[™®©]", "", value))
    return value.replace("七龍珠", "七龙珠").replace("空之軌跡", "空之轨迹")


def local_steam_app_id(name: str) -> str | None:
    root = Path(__file__).resolve().parent.parent
    path = Path(os.getenv("STEAM_DB_PATH", str(root / "data/steam_registry.db")))
    if not path.is_absolute():
        path = root / path
    if not path.is_file():
        return None
    try:
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
            ids = {str(row[0]) for row in db.execute("SELECT app_id, localized_name FROM steam_localized_game_names")
                   if same_title(name, row[1]) and str(row[0]).isdigit()}
        return next(iter(ids)) if len(ids) == 1 else None
    except sqlite3.Error:
        return None


def same_title(left: str, right: str) -> bool:
    def clean(value):
        value = re.sub(r"[™®©]", "", value)
        value = canonical_cover_title(value).casefold()
        # Store locale annotations aren't part of the game's title.
        value = re.sub(r"\([^)]*(?:语|語|language|简体中文|繁体中文|繁體中文)[^)]*\)", "", value)
        value = re.sub(r"\s+ps[45]\s*(?:&\s*ps[45])?\s*$", "", value)
        return "".join(c for c in value if c.isalnum())
    def aliases(value):
        result = {clean(value)}
        # Bilingual store labels often append a Chinese translation to the
        # complete Latin title, including Nintendo's parenthesized translation.
        match = re.fullmatch(r"([\x00-\x7f]+?)\s*[（(]?([\u3400-\u9fff\d\s]+)[）)]?", value.strip())
        if match and any("\u3400" <= c <= "\u9fff" for c in match.group(2)):
            result.add(clean(match.group(1)))
        return result - {""}
    return bool(aliases(left) & aliases(right))


async def _lookup(game) -> tuple[str, ...]:
    platform = game.platform.casefold()
    if platform in {"switch", "nintendo", "ns"}:
        from .store_matchers import NintendoStoreMatcher
        result = await NintendoStoreMatcher().lookup(game.name)
        return (result.image_url,) if same_title(game.name, result.name) and result.image_url else ()
    if platform in {"ps", "ps5", "psn", "playstation"}:
        from .personal_rank_ps_store import ps_store_cover_urls
        return await ps_store_cover_urls(game)
    known_app_id = await asyncio.to_thread(local_steam_app_id, game.name)
    if platform == "steam" or known_app_id:
        from .steam_registry import SteamClient
        client = SteamClient()
        match = re.search(r"/apps/(\d+)/", game.image_url or "")
        app_id = match.group(1) if match else known_app_id
        if app_id is None:
            for language, region in (("schinese", "cn"), ("english", "us")):
                payload = await client._public_get(
                    "https://store.steampowered.com/api/storesearch/",
                    term=canonical_cover_title(game.name), l=language, cc=region,
                )
                candidates = [item for item in payload.get("items", [])
                              if item.get("type") == "app" and same_title(game.name, item.get("name", ""))]
                if len(candidates) == 1:
                    app_id = str(candidates[0]["id"])
                    break
            if app_id is None:
                return ()
        details = await client.game_details(app_id)
        if not details.get("header_image") and not details.get("capsule_image"):
            payload = await client._public_get(
                "https://store.steampowered.com/api/appdetails", appids=app_id, l="english", cc="us",
            )
            entry = payload.get(app_id, {})
            details = entry.get("data", {}) if entry.get("success") else {}
        return tuple(dict.fromkeys(
            url for field in ("header_image", "capsule_image", "capsule_imagev5")
            if isinstance((url := details.get(field)), str) and url.startswith("https://")
        ))
    return ()


async def _grid_urls(game) -> tuple[str, ...]:
    """Use the existing artwork provider, requesting landscape grids explicitly."""
    import httpx
    import ssl
    import truststore
    from .steam_registry import SteamClient, STEAMGRIDDB_API_ROOT
    key = SteamClient()._steamgriddb_key()
    if not key:
        return ()
    lookup_name = canonical_cover_title(game.name)
    # Nintendo's port subtitle is absent from the shared artwork catalogue.
    if game.platform.casefold() == "switch" and same_title(game.name, "ELDEN RING Tarnished Edition"):
        lookup_name = "ELDEN RING"
    query = re.sub(r"[™®©]", "", re.split(r"[（(]", lookup_name, maxsplit=1)[0]).strip()
    app_id = await asyncio.to_thread(local_steam_app_id, lookup_name)
    async with httpx.AsyncClient(timeout=6, trust_env=False,
                                 verify=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT),
                                 headers={"Authorization": f"Bearer {key}"}) as client:
        if app_id:
            endpoint = f"grids/steam/{app_id}"
        else:
            response = await client.get(f"{STEAMGRIDDB_API_ROOT}/search/autocomplete/{quote(query, safe='')}")
            response.raise_for_status()
            matches = [item for item in response.json().get("data", []) if same_title(lookup_name, item.get("name", ""))]
            if not matches:
                return ()
            # Identically named original/remake entries exist (e.g. Colossus).
            # Prefer the latest verified release; do not accept fuzzy sequels.
            selected = max(matches, key=lambda item: (bool(item.get("verified")), item.get("release_date") or 0))
            endpoint = f"grids/game/{selected['id']}"
        response = await client.get(
            f"{STEAMGRIDDB_API_ROOT}/{endpoint}",
            params={"dimensions": "460x215,920x430", "types": "static", "nsfw": "false"},
        )
        response.raise_for_status()
        return tuple(item["url"] for item in response.json().get("data", [])
                     if item.get("width", 0) > item.get("height", 0)
                     and str(item.get("url", "")).startswith("https://"))[:3]


async def native_cover_urls(game, *, store_only: bool = False, grid_only: bool = False) -> tuple[str, ...]:
    key = (game.platform.casefold(), canonical_cover_title(game.name), game.image_url or "", store_only, grid_only)
    cached = _cache.get(key)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    urls = ()
    is_ps = game.platform.casefold() in {"ps", "ps5", "psn", "playstation"}
    if is_ps and not grid_only:
        try:
            urls = await asyncio.wait_for(_lookup(game), timeout=10)
        except Exception as exc:
            logger.debug("Public PS Store cover lookup unavailable: %s (%s)", game.name, type(exc).__name__)
        if urls:
            if len(_cache) >= 512:
                _cache.pop(next(iter(_cache)))
            _cache[key] = (time.monotonic() + 3600, urls)
            return urls
    if not store_only and game.platform.casefold() not in {"switch", "nintendo", "ns"}:
        try:
            urls = await asyncio.wait_for(_grid_urls(game), timeout=15)
        except Exception as exc:
            logger.debug("Native grid lookup unavailable: %s (%s)", game.name, type(exc).__name__)
    if not urls and not grid_only and not is_ps:
        try:
            urls = await asyncio.wait_for(_lookup(game), timeout=10)
        except Exception as exc:
            logger.debug("Native rank cover lookup unavailable: %s (%s)", game.name, type(exc).__name__)
    if not urls:
        logger.info("Rank cover still pending: %s; no matching landscape source", game.name)
    if len(_cache) >= 512:
        _cache.pop(next(iter(_cache)))
    _cache[key] = (time.monotonic() + (3600 if urls else 60), urls)
    return urls

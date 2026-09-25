from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import ssl
import time
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlparse

import httpx
import truststore

from .game_calendar import CalendarGame, GameLookupError, _match_score


logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_NPSSO_PATH = PROJECT_ROOT / "secrets" / "psn-npsso.txt"
NINTENDO_CATALOG_URLS = (
    "https://www.nintendo.com/hk/software/switch?sftab=all",
    "https://www.nintendo.com/hk/games/switch2/lineup?sftab=all",
)
PS_STORE_ROOT = "https://store.playstation.com/zh-hans-hk"
NINTENDO_URL_RE = re.compile(
    r"https?://(?:ec\.)?nintendo\.(?:com|com\.hk)/[^\s]*?(?:titles/)?(?P<id>\d{12,})",
    re.IGNORECASE,
)
PS_STORE_URL_RE = re.compile(
    r"https?://store\.playstation\.com/[^\s]*/(?P<kind>concept|product)/(?P<id>[^/?#\s]+)",
    re.IGNORECASE,
)


async def _fetch_text(url: str) -> str:
    last_error: Exception | None = None
    for attempt, trust_env in enumerate((False, True, True)):
        try:
            context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(35.0, connect=15.0),
                follow_redirects=True,
                verify=context,
                trust_env=trust_env,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 Chrome/128.0 Safari/537.36"
                    )
                },
            ) as client:
                response = await client.get(url)
                response.raise_for_status()
                return response.text
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            last_error = exc
            if isinstance(exc, httpx.HTTPStatusError):
                status = exc.response.status_code
                if status < 500 and status != 429:
                    break
            if attempt < 2:
                await asyncio.sleep(1 << attempt)
        except Exception as exc:
            last_error = exc
            break
    raise RuntimeError(f"store request failed: {url}") from last_error


def is_nintendo_store_url(value: str) -> bool:
    return NINTENDO_URL_RE.search(value.strip()) is not None


def is_playstation_store_url(value: str) -> bool:
    return PS_STORE_URL_RE.search(value.strip()) is not None


def _plain_html(value: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", value)).strip()


def _parse_nintendo_catalog(content: str) -> list[CalendarGame]:
    games: list[CalendarGame] = []
    seen: set[str] = set()
    anchors = re.finditer(
        r'<a\s+href="(?P<link>https://ec\.nintendo\.com/HK/zh/titles/(?P<id>\d+))"[^>]*>'
        r"(?P<body>.*?)</a>",
        content,
        re.IGNORECASE | re.DOTALL,
    )
    for anchor in anchors:
        nsuid = anchor.group("id")
        if nsuid in seen:
            continue
        body = anchor.group("body")
        name_match = re.search(
            r'class="[^"]*ncmn-softUnit__name[^"]*"[^>]*>(?P<value>.*?)</div>',
            body,
            re.IGNORECASE | re.DOTALL,
        )
        date_match = re.search(
            r'class="[^"]*ncmn-softUnit__release[^"]*"[^>]*>'
            r"(?P<year>20\d{2})\.(?P<month>\d{1,2})\.(?P<day>\d{1,2})",
            body,
            re.IGNORECASE | re.DOTALL,
        )
        image_match = re.search(
            r"background-image\s*:\s*url\((?P<quote>['\"]?)(?P<url>https?://.*?)"
            r"(?P=quote)\)",
            body,
            re.IGNORECASE | re.DOTALL,
        )
        if name_match is None or date_match is None:
            continue
        try:
            release = date(
                int(date_match.group("year")),
                int(date_match.group("month")),
                int(date_match.group("day")),
            )
        except ValueError:
            continue
        image_url = html.unescape(image_match.group("url")).strip() if image_match else None
        games.append(
            CalendarGame(
                f"switch:{nsuid}",
                _plain_html(name_match.group("value")),
                release,
                image_url,
                "Switch",
            )
        )
        seen.add(nsuid)
    return games


def _parse_nintendo_store_page(content: str, source_url: str) -> CalendarGame:
    url_match = NINTENDO_URL_RE.search(source_url)
    if url_match is None:
        raise GameLookupError("任天堂商店链接格式不正确。")

    payloads: list[Any] = []
    scripts = re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>'
        r"(?P<data>.*?)</script>",
        content,
        re.IGNORECASE | re.DOTALL,
    )
    for script in scripts:
        try:
            payloads.append(json.loads(html.unescape(script.group("data")).strip()))
        except (json.JSONDecodeError, TypeError):
            continue

    objects = [item for payload in payloads for item in _iter_dicts(payload)]
    game = next(
        (
            item
            for item in objects
            if str(item.get("@type") or "").casefold() == "videogame"
        ),
        None,
    )
    if game is None:
        raise GameLookupError("无法从该任天堂商店页面读取游戏资料。")

    name = str(game.get("name") or "").strip()
    date_values: list[object] = [game.get("datePublished")]
    offers = game.get("offers")
    if isinstance(offers, Mapping):
        date_values.append(offers.get("availabilityStarts"))
        specifications = offers.get("priceSpecification")
        if isinstance(specifications, list):
            date_values.extend(
                item.get("validFrom")
                for item in specifications
                if isinstance(item, Mapping)
            )
    release = next(
        (parsed for value in date_values if (parsed := _release_from_value(value))),
        None,
    )

    image = game.get("image")
    if isinstance(image, list):
        image = next((value for value in image if isinstance(value, str)), None)
    elif isinstance(image, Mapping):
        image = image.get("url") or image.get("contentUrl")
    image_url = (
        str(image).strip()
        if isinstance(image, str) and image.startswith(("https://", "http://"))
        else None
    )
    if not name or release is None:
        raise GameLookupError(
            "该任天堂商店页面暂无可读取的明确发售日，请在后台手动添加。"
        )
    return CalendarGame(
        f"switch:{url_match.group('id')}", name, release, image_url, "Switch"
    )


class NintendoStoreMatcher:
    def __init__(self, fetch_text: Any = _fetch_text) -> None:
        self._fetch_text = fetch_text
        self._cache: list[CalendarGame] | None = None
        self._cache_loaded_at = 0.0
        self._cache_ttl = max(float(os.getenv("NINTENDO_CATALOG_CACHE_TTL", "21600")), 60.0)
        self._lock = asyncio.Lock()

    async def _catalog(self) -> list[CalendarGame]:
        async with self._lock:
            if self._cache is not None and time.monotonic() - self._cache_loaded_at < self._cache_ttl:
                return self._cache
            results = await asyncio.gather(
                *(self._fetch_text(url) for url in NINTENDO_CATALOG_URLS),
                return_exceptions=True,
            )
            games: dict[str, CalendarGame] = {}
            for result in results:
                if isinstance(result, str):
                    for game in _parse_nintendo_catalog(result):
                        games[game.app_id] = game
                else:
                    logger.info("Nintendo catalog page unavailable: %s", type(result).__name__)
            if not games:
                if self._cache is not None:
                    logger.warning("Nintendo catalog refresh failed; using stale cache")
                    return self._cache
                raise GameLookupError("任天堂商店暂时无法查询，请稍后再试。")
            self._cache = list(games.values())
            self._cache_loaded_at = time.monotonic()
            return self._cache

    async def lookup(self, query: str) -> CalendarGame:
        direct = NINTENDO_URL_RE.search(query.strip())
        if direct:
            app_id = f"switch:{direct.group('id')}"
            detail_url = f"https://ec.nintendo.com/HK/zh/titles/{direct.group('id')}"
            detail_error = GameLookupError(
                "该任天堂商店页面暂无可读取的明确发售日，请在后台手动添加。"
            )
            try:
                return _parse_nintendo_store_page(
                    await self._fetch_text(detail_url), detail_url
                )
            except GameLookupError as exc:
                detail_error = exc
                logger.info("Nintendo product JSON-LD unavailable for %s", app_id)
            except Exception:
                logger.info("Nintendo product page unavailable for %s", app_id)

            try:
                games = await self._catalog()
            except GameLookupError:
                raise detail_error
            match = next((game for game in games if game.app_id == app_id), None)
            if match is None:
                raise detail_error
            return match
        games = await self._catalog()
        candidates = [
            (_match_score(query, game.name, position), game)
            for position, game in enumerate(games)
        ]
        if not candidates:
            raise GameLookupError("任天堂商店没有找到这个游戏。")
        score, game = max(candidates, key=lambda candidate: candidate[0])
        if score < 35:
            raise GameLookupError("任天堂商店没有找到这个游戏，请输入更完整的官方名称。")
        return game


def _iter_dicts(value: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        yield value
        for child in value.values():
            yield from _iter_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_dicts(child)


def _decode_ps_store_payloads(content: str) -> list[Any]:
    match = re.search(
        r'<script[^>]+id="__NEXT_DATA__"[^>]*>',
        content,
        re.IGNORECASE,
    )
    if match is None:
        return []
    try:
        # Batarang values can themselves contain literal </script> strings.
        # raw_decode follows the JSON object boundary instead of stopping at
        # the first inner closing tag.
        root, _ = json.JSONDecoder().raw_decode(content[match.end() :].lstrip())
    except (json.JSONDecodeError, TypeError):
        return []
    payloads: list[Any] = [root]
    page_props = root.get("props", {}).get("pageProps", {}) if isinstance(root, Mapping) else {}
    batarangs = page_props.get("batarangs", {}) if isinstance(page_props, Mapping) else {}
    if isinstance(batarangs, Mapping):
        for batarang in batarangs.values():
            if not isinstance(batarang, Mapping) or not isinstance(batarang.get("text"), str):
                continue
            inner = re.search(r"<script[^>]*>(?P<data>.*?)</script>", batarang["text"], re.DOTALL)
            if inner is None:
                continue
            try:
                payloads.append(json.loads(inner.group("data")))
            except json.JSONDecodeError:
                continue
    return payloads


def _value_id(value: object) -> str:
    return str(value or "").split(":", 1)[-1].split("#", 1)[0]


def _release_from_value(value: object) -> date | None:
    if isinstance(value, Mapping):
        value = value.get("value")
    text = str(value or "").strip()
    match = re.match(r"(?P<year>20\d{2})-(?P<month>\d{2})-(?P<day>\d{2})", text)
    if match is None:
        return None
    try:
        # PS Store timestamps represent the date displayed by the selected
        # storefront. Converting the timestamp to another timezone can move it
        # to the previous/next date, so intentionally keep the ISO date prefix.
        return date(int(match.group("year")), int(match.group("month")), int(match.group("day")))
    except ValueError:
        return None


def _best_ps_image(values: Iterable[Mapping[str, Any]]) -> str | None:
    media: list[tuple[str, str]] = []
    for value in values:
        url = value.get("url")
        role = value.get("role") or value.get("type")
        if isinstance(url, str) and url.startswith(("https://", "http://")):
            media.append((str(role or "").upper(), url))
    for preferred in (
        "FOUR_BY_THREE_BANNER",
        "GAMEHUB_COVER_ART",
        "EDITION_KEY_ART",
        "MASTER",
        "PORTRAIT_BANNER",
        # This asset is intentionally last: PS uses it as a text-free UI
        # background, so it often contains neither the game title nor logo.
        "BACKGROUND_LAYER_ART",
    ):
        match = next((url for role, url in media if role == preferred), None)
        if match:
            return match
    return media[0][1] if media else None


def _parse_ps_store_page(content: str, source_url: str) -> CalendarGame:
    url_match = PS_STORE_URL_RE.search(source_url)
    if url_match is None:
        raise GameLookupError("PlayStation Store 链接格式不正确。")
    requested_kind = url_match.group("kind").casefold()
    requested_id = url_match.group("id")
    payloads = _decode_ps_store_payloads(content)
    objects = [item for payload in payloads for item in _iter_dicts(payload)]
    concepts = [item for item in objects if item.get("__typename") == "Concept"]
    products = [item for item in objects if item.get("__typename") == "Product"]

    concept = next((item for item in concepts if _value_id(item.get("id")) == requested_id), None)
    product = next((item for item in products if _value_id(item.get("id")) == requested_id), None)
    if requested_kind == "concept" and concept is None and concepts:
        concept = concepts[0]
    if requested_kind == "product" and product is None and products:
        product = products[0]

    if concept is not None and product is None:
        default_id = _value_id(concept.get("defaultProduct"))
        if isinstance(concept.get("defaultProduct"), Mapping):
            default_id = _value_id(concept["defaultProduct"].get("id"))
        if default_id:
            product = next(
                (item for item in products if _value_id(item.get("id")) == default_id), None
            )
    selected = concept or product
    if selected is None:
        raise GameLookupError("无法从该 PlayStation Store 页面读取游戏资料。")

    name = str(selected.get("name") or (product or {}).get("name") or "").strip()
    releases = [
        _release_from_value(item.get("releaseDate"))
        for item in ([product] if product is not None else [])
        + ([concept] if concept is not None else [])
    ]
    release = next((value for value in releases if value is not None), None)
    if release is None:
        release = next(
            (
                parsed
                for item in objects
                if "releaseDate" in item
                for parsed in [_release_from_value(item.get("releaseDate"))]
                if parsed is not None
            ),
            None,
        )
    if not name or release is None:
        raise GameLookupError("该 PlayStation Store 页面尚未公布明确发售日。")

    media_objects = [item for item in objects if "url" in item]
    image_url = _best_ps_image(media_objects)
    stable_id = _value_id((concept or product).get("id"))
    return CalendarGame(f"psn:{stable_id}", name, release, image_url, "PS")


def _psn_credential_path() -> Path:
    configured = os.getenv("PSN_NPSSO_FILE", "").strip()
    if not configured:
        return DEFAULT_NPSSO_PATH
    path = Path(configured).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


class PlayStationStoreMatcher:
    def __init__(self, fetch_text: Any = _fetch_text, api: Any | None = None) -> None:
        self._fetch_text = fetch_text
        self._api = api
        self._npsso: str | None = None
        self._lock = asyncio.Lock()

    def _get_api(self) -> Any:
        if self._api is not None:
            return self._api
        try:
            from psnawp_api import PSNAWP
        except ImportError as exc:
            raise GameLookupError("PSN 商店搜索功能尚未安装。") from exc
        try:
            npsso = _psn_credential_path().read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise GameLookupError("PSN 观察账号尚未登录，暂时只能使用 PS Store 直链添加。") from exc
        if not npsso:
            raise GameLookupError("PSN 观察账号尚未登录，暂时只能使用 PS Store 直链添加。")
        self._api = PSNAWP(npsso)
        self._npsso = npsso
        return self._api

    def _search_url(self, query: str) -> str:
        try:
            from psnawp_api.models.search.games_search_datatypes import SearchDomain
        except ImportError as exc:
            raise GameLookupError("PSN 商店搜索功能尚未安装。") from exc
        results = list(self._get_api().search(query, SearchDomain.FULL_GAMES, limit=8))
        candidates: list[tuple[float, str]] = []
        for position, item in enumerate(results):
            result = item.get("result") if isinstance(item, Mapping) else None
            if not isinstance(result, Mapping):
                continue
            kind = str(result.get("__typename") or "").casefold()
            game_id = str(result.get("id") or "").strip()
            name = str(result.get("name") or "").strip()
            if kind not in {"concept", "product"} or not game_id or not name:
                continue
            candidates.append(
                (_match_score(query, name, position), f"{PS_STORE_ROOT}/{kind}/{game_id}")
            )
        if not candidates:
            raise GameLookupError("PlayStation Store 没有找到这个游戏。")
        score, url = max(candidates, key=lambda candidate: candidate[0])
        if score < 35:
            raise GameLookupError("PlayStation Store 没有找到这个游戏，请输入更完整的官方名称。")
        return url

    async def lookup(self, query: str) -> CalendarGame:
        direct = PS_STORE_URL_RE.search(query.strip())
        if direct:
            url = f"{PS_STORE_ROOT}/{direct.group('kind').casefold()}/{direct.group('id')}"
        else:
            async with self._lock:
                try:
                    url = await asyncio.to_thread(self._search_url, query)
                except GameLookupError:
                    raise
                except Exception as exc:
                    raise GameLookupError("PlayStation Store 暂时无法搜索，请稍后再试。") from exc
        try:
            content = await self._fetch_text(url)
            return _parse_ps_store_page(content, url)
        except GameLookupError:
            raise
        except Exception as exc:
            raise GameLookupError("PlayStation Store 暂时无法查询，请稍后再试。") from exc

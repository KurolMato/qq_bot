"""Public PS Store artwork search, independent of the trophy observer's login."""
from __future__ import annotations

import json
import ssl
from urllib.parse import urlsplit

import httpx
import truststore

from .personal_rank_sources import canonical_cover_title, same_title

SEARCH_URL = "https://web.np.playstation.com/api/graphql/v1/op"
# getSearchResults from the public PS Store web client (2026-09-26).
# The endpoint only accepts Sony's persisted, whitelisted operations.
SEARCH_HASH = "4df6284f982e57bec70f23c77e2c219dc792eb19af7fb3d3a81767aa3f1958aa"
ART_ROLES = ("SIXTEEN_BY_NINE_BANNER", "GAMEHUB_COVER_ART",
             "FOUR_BY_THREE_BANNER", "BACKGROUND", "BACKGROUND_LAYER_ART")


def _image_identity(url: str) -> str | None:
    parsed = urlsplit(url)
    if parsed.hostname != "image.api.playstation.com":
        return None
    return parsed.path


def _select_art(results: list, name: str, original_url: str | None) -> tuple[str, ...]:
    original = _image_identity(original_url or "")
    candidates = []
    for item in results:
        if not isinstance(item, dict) or item.get("__typename") not in {"Concept", "Product"}:
            continue
        media = [m for m in item.get("media", []) if isinstance(m, dict)]
        # An exact Sony asset identity links translated titles without accepting
        # an unrelated fuzzy search hit, sequel or DLC.
        art_match = bool(original and any(_image_identity(m.get("url", "")) == original for m in media))
        title_match = same_title(name, item.get("name", ""))
        if title_match or art_match:
            candidates.append((not title_match, media))
    candidates.sort(key=lambda item: item[0])
    urls = []
    for role in ART_ROLES:
        for _, media in candidates:
            for entry in media:
                url = entry.get("url", "")
                if entry.get("role") == role and entry.get("type") == "IMAGE" and url.startswith("https://") and url not in urls:
                    urls.append(url)
    # The image loader still decodes and checks the actual landscape dimensions.
    return tuple(urls[:5])


async def ps_store_cover_urls(game) -> tuple[str, ...]:
    async with httpx.AsyncClient(
        trust_env=False, timeout=8,
        verify=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT),
        headers={"Content-Type": "application/json", "x-psn-store-locale-override": "zh-Hans-HK"},
    ) as client:
        response = await client.get(SEARCH_URL, params={
            "operationName": "getSearchResults",
            "variables": json.dumps({"countryCode": "HK", "languageCode": "zh",
                "pageOffset": 0, "pageSize": 24, "nextCursor": "", "searchTerm": canonical_cover_title(game.name)}),
            "extensions": json.dumps({"persistedQuery": {"version": 1, "sha256Hash": SEARCH_HASH}}),
        })
        response.raise_for_status()
        payload = response.json()
        if payload.get("errors"):
            raise ValueError("PS Store public search returned errors")
        results = payload.get("data", {}).get("universalSearch", {}).get("results", [])
        return _select_art(results, game.name, game.image_url)

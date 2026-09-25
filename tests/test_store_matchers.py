import asyncio
import json
from datetime import date

from qq_bot.store_matchers import (
    NintendoStoreMatcher,
    _parse_nintendo_catalog,
    _parse_nintendo_store_page,
    _parse_ps_store_page,
    is_nintendo_store_url,
    is_playstation_store_url,
)


NINTENDO_HTML = """
<a href="https://ec.nintendo.com/HK/zh/titles/70010000080764" class="ncmn-softUnit">
  <div class="ncmn-softUnit__thumb">
    <div class="ncmn-thumb" style="background-image:url(https://images.example/cover.jpg?x=1&amp;y=2)"></div>
  </div>
  <div class="ncmn-softUnit__name">空之軌跡 the 2nd</div>
  <div class="ncmn-softUnit__hardware">Nintendo Switch</div>
  <div class="ncmn-softUnit__release">2026.9.17</div>
</a>
"""


NINTENDO_DETAIL_HTML = """
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "VideoGame",
  "gamePlatform": "Nintendo Switch 2",
  "image": "https://img-eshop.cdn.nintendo.net/i/orbitals.jpg",
  "name": "軌道雙子星",
  "offers": {
    "@type": "Offer",
    "priceSpecification": [{"validFrom": "2026-09-03"}],
    "availabilityStarts": "2026-09-03"
  },
  "datePublished": "2026-09-03"
}
</script>
"""


def _ps_page() -> str:
    cache = {
        "Concept:10002684": {
            "__typename": "Concept",
            "id": "10002684",
            "name": "宇宙机器人",
            "defaultProduct": {"id": "UP9000-PPSA21564_00-ASTROBOT00000000"},
        },
        "Product:UP9000-PPSA21564_00-ASTROBOT00000000": {
            "__typename": "Product",
            "id": "UP9000-PPSA21564_00-ASTROBOT00000000",
            "name": "宇宙机器人 标准版",
            "releaseDate": "2024-09-05T16:00:00Z",
            "media": [
                {
                    "role": "FOUR_BY_THREE_BANNER",
                    "url": "https://image.api.playstation.com/astro-logo-banner.jpg",
                },
                {
                    "role": "BACKGROUND_LAYER_ART",
                    "url": "https://image.api.playstation.com/astro-background.jpg",
                }
            ],
        },
    }
    inner = json.dumps({"cache": cache}, ensure_ascii=False)
    root = {
        "props": {
            "pageProps": {
                "batarangs": {
                    "info": {"text": f'<script type="application/json">{inner}</script>'}
                }
            }
        }
    }
    return (
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(root, ensure_ascii=False)
        + "</script>"
    )


def test_nintendo_catalog_reads_official_name_date_and_image() -> None:
    games = _parse_nintendo_catalog(NINTENDO_HTML)

    assert games == [
        games[0].__class__(
            "switch:70010000080764",
            "空之軌跡 the 2nd",
            date(2026, 9, 17),
            "https://images.example/cover.jpg?x=1&y=2",
            "Switch",
        )
    ]


def test_nintendo_detail_reads_json_ld_name_date_and_image() -> None:
    game = _parse_nintendo_store_page(
        NINTENDO_DETAIL_HTML,
        "https://ec.nintendo.com/HK/zh/titles/70010000098977",
    )

    assert game.app_id == "switch:70010000098977"
    assert game.name == "軌道雙子星"
    assert game.release_date == date(2026, 9, 3)
    assert game.image_url == "https://img-eshop.cdn.nintendo.net/i/orbitals.jpg"


def test_nintendo_direct_link_prefers_product_json_ld() -> None:
    requested: list[str] = []

    async def fetch(url: str) -> str:
        requested.append(url)
        return NINTENDO_DETAIL_HTML

    game = asyncio.run(
        NintendoStoreMatcher(fetch).lookup(
            "https://ec.nintendo.com/HK/zh/titles/70010000098977"
        )
    )

    assert game.name == "軌道雙子星"
    assert game.release_date == date(2026, 9, 3)
    assert requested == ["https://ec.nintendo.com/HK/zh/titles/70010000098977"]


def test_nintendo_matcher_accepts_direct_eshop_link() -> None:
    async def fetch(_url: str) -> str:
        return NINTENDO_HTML

    matcher = NintendoStoreMatcher(fetch)
    game = asyncio.run(
        matcher.lookup("https://ec.nintendo.com/HK/zh/titles/70010000080764")
    )

    assert game.name == "空之軌跡 the 2nd"
    assert game.platform == "Switch"


def test_nintendo_catalog_cache_refreshes_after_ttl() -> None:
    calls = 0

    async def fetch(_url: str) -> str:
        nonlocal calls
        calls += 1
        return NINTENDO_HTML

    matcher = NintendoStoreMatcher(fetch)
    asyncio.run(matcher.lookup("空之軌跡"))
    first_calls = calls
    matcher._cache_loaded_at -= matcher._cache_ttl + 1
    asyncio.run(matcher.lookup("空之軌跡"))

    assert first_calls == 2
    assert calls == 4


def test_ps_store_parser_keeps_store_date_without_timezone_shift() -> None:
    game = _parse_ps_store_page(
        _ps_page(), "https://store.playstation.com/zh-hans-hk/concept/10002684"
    )

    assert game.app_id == "psn:10002684"
    assert game.name == "宇宙机器人"
    assert game.release_date == date(2024, 9, 5)
    assert game.image_url == "https://image.api.playstation.com/astro-logo-banner.jpg"
    assert game.platform == "PS"


def test_store_url_detection() -> None:
    assert is_nintendo_store_url(
        "https://ec.nintendo.com/HK/zh/titles/70010000080764"
    )
    assert is_playstation_store_url(
        "https://store.playstation.com/zh-hans-hk/product/UP9000-PPSA21564_00-ASTROBOT00000000"
    )
    assert not is_playstation_store_url("宇宙机器人")

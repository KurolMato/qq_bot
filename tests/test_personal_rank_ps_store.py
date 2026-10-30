import asyncio
from types import SimpleNamespace as NS
import httpx
import pytest
from qq_bot import personal_rank_ps_store as store


def media(role, suffix):
    return {'role':role,'type':'IMAGE','url':'https://image.api.playstation.com/art/'+suffix}


def test_translated_title_matches_original_sony_asset_not_first_search_hit():
    wrong={'__typename':'Concept','name':'Other Game','media':[media('SIXTEEN_BY_NINE_BANNER','wrong.jpg')]}
    right={'__typename':'Product','name':'STEINS;GATE RE:BOOT (英语, 中文)',
           'media':[media('MASTER','original.png'),media('BACKGROUND','background.jpg'),media('SIXTEEN_BY_NINE_BANNER','banner.jpg')]}
    urls=store._select_art([wrong,right],'命运石之门 RE:BOOT','https://image.api.playstation.com/art/original.png?w=100')
    assert urls==('https://image.api.playstation.com/art/banner.jpg','https://image.api.playstation.com/art/background.jpg')


def test_exact_localized_title_gets_background_when_old_game_has_no_banner():
    game={'__typename':'Product','name':'十三机兵防卫圈 (韩语, 简体中文, 繁体中文)',
          'media':[media('MASTER','square.jpg'),media('BACKGROUND','landscape.jpg')]}
    assert store._select_art([game],'十三机兵防卫圈',None)==('https://image.api.playstation.com/art/landscape.jpg',)


def test_sequel_and_dlc_are_not_accepted_from_fuzzy_results():
    results=[{'__typename':'Product','name':name,'media':[media('SIXTEEN_BY_NINE_BANNER','wrong.jpg')]}
             for name in ('Game 2','Game Deluxe Upgrade','Game - Chapter Pack')]
    assert store._select_art(results,'Game',None)==()


def test_public_request_needs_no_psn_credentials(monkeypatch):
    original_client=httpx.AsyncClient
    def handle(request):
        assert request.headers['content-type']=='application/json'
        assert 'authorization' not in request.headers
        assert request.url.params['operationName']=='getSearchResults'
        return httpx.Response(200,json={'data':{'universalSearch':{'results':[
            {'__typename':'Concept','name':'Game','media':[media('SIXTEEN_BY_NINE_BANNER','banner.jpg')]}
        ]}}})
    monkeypatch.setattr(store.httpx,'AsyncClient',lambda **kwargs:original_client(**kwargs,transport=httpx.MockTransport(handle)))
    assert asyncio.run(store.ps_store_cover_urls(NS(name='Game',image_url=None)))==('https://image.api.playstation.com/art/banner.jpg',)


def test_public_api_errors_are_not_treated_as_success(monkeypatch):
    original_client=httpx.AsyncClient
    monkeypatch.setattr(store.httpx,'AsyncClient',lambda **kwargs:original_client(**kwargs,transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'errors':[{'message':'unavailable'}]}))))
    with pytest.raises(ValueError):
        asyncio.run(store.ps_store_cover_urls(NS(name='Game',image_url=None)))

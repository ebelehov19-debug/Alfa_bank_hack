import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

from bs4 import BeautifulSoup
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from news_parser.extract import listing_candidates
from news_parser.models import Settings
from news_parser.transport import Transport

RAW = (Path(__file__).parent / 'fixtures/tass_feed.html').read_text(encoding='utf-8')
URL = 'https://tass.ru/ekonomika'
CSS = '''<style>
body {margin:0} [class*="CardsGrid_cards"] a {display:block; height:65px}
[class*="InfiniteLoading_container"] {height:50px}
</style>'''


def test_real_feed_has_30_links_and_sentinel_above_artificial_page_bottom():
    soup = BeautifulSoup(RAW, 'html.parser')
    loader = soup.select_one('[class*="InfiniteLoading_container"]')
    assert loader and '5000vh' in loader.parent['style']
    candidates = listing_candidates(RAW, URL, 'tass')
    assert len(candidates) == 30 and all(c.categories == ['Экономика и бизнес'] for c in candidates)
    outside = '<aside><a href="/ekonomika/99999999">Чужая карточка</a></aside>'
    assert len(listing_candidates(RAW + outside, URL, 'tass')) == 30


@pytest.mark.browser
@pytest.mark.parametrize('nested,replace', [(False,False), (True,False), (False,True)])
def test_waits_for_slow_new_ids_and_scrolls_real_sentinel(monkeypatch,nested,replace):
    for name in ('HTTPS_PROXY','HTTP_PROXY','ALL_PROXY','https_proxy','http_proxy','all_proxy'):
        monkeypatch.delenv(name,raising=False)
    raw = RAW
    if nested:
        raw = raw.replace('<body>','<body><div style="height:350px;overflow-y:auto">').replace('</body>','</div></body>')
    script = '''<script>
    window.loads=0;
    const sentinel=document.querySelector('[class*="InfiniteLoading_container"]');
    const grid=document.querySelector('[class*="CardsGrid_cards"]');
    new IntersectionObserver(entries=>{
      if (!entries.some(e=>e.isIntersecting) || window.loads) return;
      window.loads++;
      setTimeout(()=>{
        if (REPLACE) grid.replaceChildren();
        for(let i=0;i<2;i++){
          const a=document.createElement('a'); a.href='/ekonomika/'+(99000000+i);
          a.textContent='Новая статья '+i; grid.append(a);
        }
      },1200);
    }).observe(sentinel);
    </script>'''.replace('REPLACE','true' if replace else 'false')
    raw = raw.replace('</head>',CSS+'</head>').replace('</body>',script+'</body>')
    async def scenario():
        async with Transport(Settings(timeout=3,browser_timeout=5,delay=0,max_pages=4)) as t:
            await t._ensure_browser()
            async def route(r):
                await r.fulfill(status=200,content_type='text/html; charset=utf-8',body=raw)
            await t._context.route('**/*',route)
            now=datetime.now(timezone.utc)
            pages=await t.tass_listing(URL,'tass',32,now-timedelta(hours=48))
            ids={c.url for p in pages for c in listing_candidates(p,URL,'tass')}
            assert len(ids)==32 and len(pages)==2
            assert 'https://tass.ru/ekonomika/99000001' in ids
    asyncio.run(scenario())


@pytest.mark.browser
def test_duplicate_cards_do_not_signal_progress_or_loop_forever(monkeypatch):
    for name in ('HTTPS_PROXY','HTTP_PROXY','ALL_PROXY','https_proxy','http_proxy','all_proxy'):
        monkeypatch.delenv(name,raising=False)
    script='''<script>
      const grid=document.querySelector('[class*="CardsGrid_cards"]');
      new IntersectionObserver(entries=>{
        if(entries.some(e=>e.isIntersecting)) grid.append(grid.querySelector('a').cloneNode(true));
      }).observe(document.querySelector('[class*="InfiniteLoading_container"]'));
    </script>'''
    raw=RAW.replace('</head>',CSS+'</head>').replace('</body>',script+'</body>')
    async def scenario():
        async with Transport(Settings(timeout=.35,browser_timeout=3,delay=0,max_pages=20)) as t:
            await t._ensure_browser()
            async def route(r):
                await r.fulfill(status=200,content_type='text/html; charset=utf-8',body=raw)
            await t._context.route('**/*',route)
            pages=await t.tass_listing(URL,'tass',200,datetime.now(timezone.utc)-timedelta(hours=48))
            assert len(pages)==1 and len(listing_candidates(pages[0],URL,'tass'))==30
    asyncio.run(scenario())


@pytest.mark.browser
def test_max_pages_one_does_not_scroll_or_drop_initial_links(monkeypatch):
    for name in ('HTTPS_PROXY','HTTP_PROXY','ALL_PROXY','https_proxy','http_proxy','all_proxy'):
        monkeypatch.delenv(name,raising=False)
    async def scenario():
        async with Transport(Settings(timeout=2,browser_timeout=3,delay=0,max_pages=1)) as t:
            await t._ensure_browser()
            async def route(r):
                await r.fulfill(status=200,content_type='text/html; charset=utf-8',body=RAW)
            await t._context.route('**/*',route)
            pages=await t.tass_listing(URL,'tass',200,datetime.now(timezone.utc)-timedelta(hours=48))
            assert len(pages)==1 and len(listing_candidates(pages[0],URL,'tass'))==30
    asyncio.run(scenario())

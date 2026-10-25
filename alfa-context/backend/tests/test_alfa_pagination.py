"""Offline browser checks for Alfa's JavaScript numbered pagination."""
import asyncio
from datetime import datetime, timedelta
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from news_parser.alfa_extract import alfa_listing_candidates
from news_parser.models import Settings, UTC
from news_parser.transport import FetchError, Transport


URL = 'https://alfabank.ru/news/t/release/'
NOW = datetime(2026, 10, 24, 12, tzinfo=UTC)


def listing(*, delay=0, change=True, day='23 октября', deny=False):
    # The a0N596/e0N596/f0N596 classes and numbered buttons are confirmed by
    # the official CpasPressReleasesDesktop.410c791d.es6.js asset. Card markup
    # here is synthetic so tests remain independent of live access checks.
    next_html = '<article><a href="/news/t/release/second/">Second release</a><p>22 октября</p></article>'
    update = ("const heading=document.createElement('h1'); heading.textContent='Forbidden'; "
              "document.querySelector('main').replaceChildren(heading);" if deny else
              f"document.querySelector('#cards').innerHTML={next_html!r};") if change else ''
    return f'''<!doctype html><html><body><main>
      <section id="cards"><article>
        <a href="/news/t/release/first/">First release</a><p>{day}</p>
      </article></section>
      <ul><li class="a0N596 b0N596"><button id="one"><span class="a0N596 e0N596 f0N596">1</span></button></li>
      <li class="a0N596 b0N596"><button id="two" aria-label="Страница 2"><span class="a0N596 e0N596">2</span></button></li>
      <li class="a0N596 b0N596"><button disabled aria-label="Страница 3">3</button></li></ul>
      </main><script>
        window.clicks=0;
        document.querySelector('#two').onclick=()=>{{
          window.clicks++;
          setTimeout(()=>{{{update}}},{delay});
        }};
      </script></body></html>'''


def no_proxy(monkeypatch):
    for key in ('HTTPS_PROXY', 'HTTP_PROXY', 'ALL_PROXY', 'https_proxy', 'http_proxy', 'all_proxy'):
        monkeypatch.delenv(key, raising=False)


async def prepare(transport, raw):
    context = await transport._browser_context(URL)
    async def route(request_route):
        await request_route.fulfill(status=200, content_type='text/html; charset=utf-8', body=raw)
    await context.route('**/*', route)
    opened = []
    original_open = transport._open
    async def capture(url):
        page = await original_open(url)
        opened.append(page)
        return page
    transport._open = capture
    return opened


@pytest.mark.browser
@pytest.mark.parametrize('delay', [0, 1200])
def test_numbered_buttons_wait_for_changed_links_and_stop_at_disabled_end(monkeypatch, delay):
    no_proxy(monkeypatch)
    async def scenario():
        async with Transport(Settings(timeout=3, browser_timeout=4, delay=0, max_pages=10)) as transport:
            opened = await prepare(transport, listing(delay=delay))
            stream = transport.alfa_pages(URL, NOW-timedelta(days=7), NOW)
            first = await anext(stream)
            assert await opened[0].evaluate('window.clicks') == 0
            second = await anext(stream)
            with pytest.raises(StopAsyncIteration):
                await anext(stream)
            assert [alfa_listing_candidates(raw, URL, now=NOW)[0].title for raw in (first, second)] == [
                'First release', 'Second release']
            assert opened[0].is_closed()
    asyncio.run(scenario())


@pytest.mark.browser
def test_early_generator_close_does_not_load_another_page(monkeypatch):
    no_proxy(monkeypatch)
    async def scenario():
        async with Transport(Settings(timeout=2, browser_timeout=3, delay=0)) as transport:
            opened = await prepare(transport, listing())
            stream = transport.alfa_pages(URL, NOW-timedelta(days=7), NOW)
            await anext(stream)
            assert await opened[0].evaluate('window.clicks') == 0
            await stream.aclose()
            assert opened[0].is_closed()
    asyncio.run(scenario())


@pytest.mark.browser
def test_unchanged_links_fail_with_bounded_wait_and_close_page(monkeypatch):
    no_proxy(monkeypatch)
    async def scenario():
        async with Transport(Settings(timeout=2, browser_timeout=.7, delay=0)) as transport:
            opened = await prepare(transport, listing(change=False))
            stream = transport.alfa_pages(URL, NOW-timedelta(days=7), NOW)
            await anext(stream)
            with pytest.raises(FetchError, match='did not change or settle'):
                await anext(stream)
            assert opened[0].is_closed()
    asyncio.run(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('old, max_pages', [(True, 10), (False, 1)])
def test_old_pages_and_page_limit_stop_without_clicking(monkeypatch, old, max_pages):
    no_proxy(monkeypatch)
    async def scenario():
        async with Transport(Settings(timeout=2, browser_timeout=3, delay=0, max_pages=max_pages)) as transport:
            opened = await prepare(transport, listing(day='01 октября' if old else '23 октября'))
            stream = transport.alfa_pages(URL, NOW-timedelta(days=7), NOW)
            await anext(stream)
            assert await opened[0].evaluate('window.clicks') == 0
            with pytest.raises(StopAsyncIteration):
                await anext(stream)
            assert opened[0].is_closed()
    asyncio.run(scenario())


@pytest.mark.browser
def test_access_denied_after_pagination_is_reported_and_closed(monkeypatch):
    no_proxy(monkeypatch)
    async def scenario():
        async with Transport(Settings(timeout=2, browser_timeout=3, delay=0)) as transport:
            opened = await prepare(transport, listing(deny=True))
            stream = transport.alfa_pages(URL, NOW-timedelta(days=7), NOW)
            await anext(stream)
            with pytest.raises(FetchError, match='Alfa denied access'):
                await anext(stream)
            assert opened[0].is_closed()
    asyncio.run(scenario())


def test_pagination_rejects_another_source_before_opening_browser():
    async def scenario():
        async with Transport(Settings(browser=False)) as transport:
            stream = transport.alfa_pages('https://tass.ru/ekonomika', NOW-timedelta(days=7), NOW)
            with pytest.raises(ValueError, match='not an Alfa release'):
                await anext(stream)
            assert transport._context is None
    asyncio.run(scenario())

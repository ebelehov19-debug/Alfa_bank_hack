"""Alfa's certificate exception must remain confined to its own sessions."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import ssl
import sys

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from news_parser.models import Settings
from news_parser.transport import FetchError, Transport


ALFA = 'https://alfabank.ru/news/t/release/'
TASS = 'https://tass.ru/ekonomika'
MOEX = 'https://www.moex.com/ru/news/'
LISTING = '<main><a href="/news/t/release/a-release/">Alfa release</a></main>'


def cookie(domain, value):
    return {'name': 'visitor', 'value': value, 'domain': domain, 'path': '/',
            'expires': -1, 'httpOnly': False, 'secure': True, 'sameSite': 'Lax'}


def origin(host, value):
    return {'origin': f'https://{host}',
            'localStorage': [{'name': 'visitor', 'value': value}]}


class OfflinePage:
    def __init__(self, raw=LISTING):
        self.raw = raw
        self.url = 'about:blank'
        self.main_frame = object()
        self.closed = False

    def on(self, *_):
        pass

    async def goto(self, url, **_):
        self.url = url

    async def content(self):
        return self.raw

    async def close(self):
        self.closed = True


class OfflineContext:
    def __init__(self, options):
        self.options = deepcopy(options)
        self.state = deepcopy(options.get('storage_state', {'cookies': [], 'origins': []}))
        self.routes = []
        self.closed = False

    def set_default_timeout(self, _):
        pass

    async def route(self, pattern, handler):
        self.routes.append((pattern, handler))

    async def new_page(self):
        return OfflinePage()

    async def cookies(self, urls=None):
        return deepcopy(self.state['cookies'])

    async def storage_state(self):
        return deepcopy(self.state)

    async def close(self):
        self.closed = True


class OfflineBrowser:
    version = '130.0.0.0'

    def __init__(self):
        self.contexts = []
        self.closed = False

    async def new_context(self, **options):
        context = OfflineContext(options)
        self.contexts.append(context)
        return context

    async def close(self):
        self.closed = True


@pytest.fixture
def offline_browser(monkeypatch):
    from playwright import async_api

    browser = OfflineBrowser()

    class Chromium:
        async def launch(self, **_):
            return browser

    class Playwright:
        chromium = Chromium()

        async def stop(self):
            pass

    class Starter:
        async def start(self):
            return Playwright()

    monkeypatch.setattr(async_api, 'async_playwright', Starter)
    return browser


class Route:
    def __init__(self, url, kind='script'):
        self.request = type('Request', (), {
            'url': url, 'resource_type': kind,
            'is_navigation_request': lambda self: kind == 'document',
            'frame': type('Frame', (), {'parent_frame': None})(),
        })()
        self.action = None

    async def abort(self):
        self.action = 'abort'

    async def continue_(self):
        self.action = 'continue'


def test_default_http_certificate_exception_is_only_for_exact_alfa_hosts():
    async def scenario():
        async with Transport(Settings()) as transport:
            strict = transport.client
            alfa = transport._alfa_client
            assert strict is not alfa
            assert strict._transport._pool._ssl_context.verify_mode == ssl.CERT_REQUIRED
            assert alfa._transport._pool._ssl_context.verify_mode == ssl.CERT_NONE
            for url in (ALFA, ALFA.replace('alfabank.ru', 'www.alfabank.ru')):
                assert transport._http_client(url) is alfa
            for url in (TASS, MOEX, 'https://iss.moex.com/iss/sitenews.json',
                        'https://alfabank.ru.evil.test/', 'https://evil-alfabank.ru/'):
                assert transport._http_client(url) is strict
    asyncio.run(scenario())


def test_requests_and_same_source_redirects_use_separate_http_clients():
    strict_requests, alfa_requests = [], []

    def strict_handler(request):
        strict_requests.append(str(request.url))
        return httpx.Response(200, text='strict document')

    def alfa_handler(request):
        alfa_requests.append(str(request.url))
        if request.url.host == 'www.alfabank.ru':
            return httpx.Response(302, headers={'location': ALFA})
        return httpx.Response(200, text='Alfa document')

    async def scenario():
        async with Transport(Settings(delay=0, retries=0),
                             client=httpx.AsyncClient(transport=httpx.MockTransport(strict_handler)),
                             alfa_client=httpx.AsyncClient(transport=httpx.MockTransport(alfa_handler))) as transport:
            assert await transport.get(ALFA.replace('alfabank.ru', 'www.alfabank.ru')) == 'Alfa document'
            assert await transport.get(TASS) == 'strict document'
            assert await transport.get(MOEX) == 'strict document'
    asyncio.run(scenario())
    assert alfa_requests == [ALFA.replace('alfabank.ru', 'www.alfabank.ru'), ALFA]
    assert strict_requests == [TASS, MOEX]


@pytest.mark.parametrize('start,destination', [(ALFA, TASS), (TASS, ALFA)])
def test_redirect_cannot_transfer_certificate_exception_to_another_source(start, destination):
    visited = []

    def handler(request):
        visited.append(str(request.url))
        return httpx.Response(302, headers={'location': destination})

    async def scenario():
        async with Transport(Settings(delay=0, retries=0),
                             client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                             alfa_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))) as transport:
            with pytest.raises(FetchError, match='Redirect to another source'):
                await transport.get(start)
    asyncio.run(scenario())
    assert visited == [start]


def test_imported_http_sessions_partition_cookies_and_preserve_user_agent(tmp_path):
    state = tmp_path / 'state.json'
    state.write_text(json.dumps({'cookies': [cookie('tass.ru', 'strict'), cookie('alfabank.ru', 'alfa')],
                                 'origins': [], '_news_parser': {'user_agent': 'VerifiedBrowser/1'}}))
    observed = {}

    def handler(request):
        observed[request.url.host] = dict(request.headers)
        return httpx.Response(200, text='document')

    async def scenario():
        async with Transport(Settings(browser_state=str(state), delay=0),
                             client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                             alfa_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))) as transport:
            await transport.get(TASS)
            await transport.get(ALFA)
            assert {item.domain for item in transport.client.cookies.jar} == {'tass.ru'}
            assert {item.domain for item in transport._alfa_client.cookies.jar} == {'alfabank.ru'}
    asyncio.run(scenario())
    assert observed['tass.ru']['cookie'] == 'visitor=strict'
    assert observed['alfabank.ru']['cookie'] == 'visitor=alfa'
    assert all(headers['user-agent'] == 'VerifiedBrowser/1' for headers in observed.values())


def test_browser_contexts_keep_strict_tls_and_use_partitioned_saved_state(tmp_path, offline_browser):
    state = tmp_path / 'state.json'
    state.write_text(json.dumps({'cookies': [cookie('tass.ru', 'tass'), cookie('alfabank.ru', 'alfa'),
                                             cookie('evil.test', 'alien')],
                                 'origins': [origin('tass.ru', 'tass'), origin('alfabank.ru', 'alfa'),
                                             origin('evil.test', 'alien')]}))

    async def scenario():
        async with Transport(Settings(browser_state=str(state))) as transport:
            strict = await transport._browser_context(TASS)
            alfa = await transport._browser_context(ALFA)
            assert strict is await transport._browser_context(MOEX)
            assert alfa is await transport._browser_context(ALFA.replace('alfabank.ru', 'www.alfabank.ru'))
            assert strict is not alfa
            assert not strict.options.get('ignore_https_errors', False)
            assert alfa.options.get('ignore_https_errors') is True
            # Preserve unrelated existing session entries in the strict context;
            # only Alfa's first-party state moves to the certificate exception.
            assert {item['domain'] for item in strict.state['cookies']} == {'tass.ru', 'evil.test'}
            assert {item['domain'] for item in alfa.state['cookies']} == {'alfabank.ru'}
            assert {item['origin'] for item in strict.state['origins']} == {'https://tass.ru', 'https://evil.test'}
            assert {item['origin'] for item in alfa.state['origins']} == {'https://alfabank.ru'}
        assert all(context.closed for context in offline_browser.contexts)
        assert offline_browser.closed
    asyncio.run(scenario())


def test_alfa_browser_route_cannot_load_other_sources_with_tls_exception(offline_browser):
    async def scenario():
        async with Transport(Settings()) as transport:
            alfa = await transport._browser_context(ALFA)
            handler = alfa.routes[0][1]
            for url in (ALFA, 'https://www.alfabank.ru/news/t/release/',
                        'https://alfabank.servicecdn.ru/assr/component.js',
                        'https://servicepipe.tech/check.js', 'https://captcha.servicepipe.tech/frame'):
                route = Route(url)
                await handler(route)
                assert route.action == 'continue', url
            for url in (TASS, MOEX, 'https://iss.moex.com/iss/sitenews.json',
                        'https://alfabank.ru.evil.test/script.js', 'https://other.servicecdn.ru/script.js'):
                route = Route(url, 'document')
                await handler(route)
                assert route.action == 'abort', url
    asyncio.run(scenario())


def test_verified_browser_cookies_are_copied_to_the_corresponding_http_client(offline_browser):
    observed = []

    def handler(request):
        observed.append((request.url.host, request.headers.get('cookie', '')))
        return httpx.Response(200, text='document')

    async def scenario():
        async with Transport(Settings(delay=0),
                             client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                             alfa_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))) as transport:
            context = await transport._browser_context(ALFA)
            context.state['cookies'] = [cookie('alfabank.ru', 'fresh-alfa')]
            assert await transport.render(ALFA) == LISTING
            await transport.get(ALFA)
            await transport.get(TASS)
            assert not list(transport.client.cookies.jar)
            assert {item.domain for item in transport._alfa_client.cookies.jar} == {'alfabank.ru'}
    asyncio.run(scenario())
    assert observed == [('alfabank.ru', 'visitor=fresh-alfa'), ('tass.ru', '')]


@pytest.mark.parametrize('updated', [('alfa',), ('tass',), ('tass', 'alfa')])
def test_saving_session_keeps_other_source_state_and_replaces_updated_values(tmp_path, offline_browser, updated):
    state = tmp_path / 'state.json'
    state.write_text(json.dumps({'cookies': [cookie('tass.ru', 'old-tass'), cookie('alfabank.ru', 'old-alfa')],
                                 'origins': [origin('tass.ru', 'old-tass'), origin('alfabank.ru', 'old-alfa')]}))

    async def scenario():
        async with Transport(Settings(browser_state=str(state))) as transport:
            for source in updated:
                host = 'alfabank.ru' if source == 'alfa' else 'tass.ru'
                context = await transport._browser_context(ALFA if source == 'alfa' else TASS)
                context.state = {'cookies': [cookie(host, f'fresh-{source}')],
                                 'origins': [origin(host, f'fresh-{source}')]}
                transport._verified.add(host)
    asyncio.run(scenario())
    saved = json.loads(state.read_text())
    expected = {host: f'{"fresh" if source in updated else "old"}-{source}'
                for host, source in [('tass.ru', 'tass'), ('alfabank.ru', 'alfa')]}
    assert {item['domain']: item['value'] for item in saved['cookies']} == expected
    assert len(saved['cookies']) == 2
    assert {item['origin'].removeprefix('https://'): item['localStorage'][0]['value']
            for item in saved['origins']} == expected
    assert saved['_news_parser']['user_agent']

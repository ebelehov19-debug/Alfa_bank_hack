import asyncio
import json
from pathlib import Path
import sys

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from news_parser.access import (browser_request_allowed, has_source_content,
                                is_challenge)
from news_parser.extract import article_from_html
from news_parser.models import Candidate, Settings, canonical_url, request_url
from news_parser.transport import FetchError, Transport


@pytest.mark.parametrize('host', ['servicepipe.tech', 'static.servicepipe.tech', 'captcha.servicepipe.tech'])
def test_access_scripts_images_and_frames_are_allowed(host):
    for kind in ('script', 'image', 'document', 'xhr', 'fetch'):
        assert browser_request_allowed(f'https://{host}/check.js', kind)


@pytest.mark.parametrize('host', ['servicepipe.tech.evil.test', 'evil-servicepipe.tech', '127.0.0.1'])
def test_allowlist_still_rejects_other_domains(host):
    assert not browser_request_allowed(f'https://{host}/check.js', 'script')


def test_preserve_request_host_case_and_signed_query_order():
    value = 'https://www.tass.ru/ekonomika/42?EventId=AbC-B-B&sig=X%2BY&attempt=1'
    assert request_url(value) == value
    assert canonical_url(value) != value
    with pytest.raises(ValueError):
        request_url('https://tass.ru.evil.test/')


def test_servicepipe_200_is_not_an_article_or_completed_page():
    raw = '<body><js-challenge-loader></js-challenge-loader><script>function get_cookie_spid(){return "x";}</script></body>'
    assert is_challenge(raw)
    assert not has_source_content(raw, 'https://tass.ru/ekonomika/1')
    with pytest.raises(ValueError):
        article_from_html(raw, 'tass', Candidate('https://tass.ru/ekonomika/1'))

    async def scenario():
        async with Transport(Settings(), client=httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text=raw)))) as t:
            with pytest.raises(FetchError, match='JavaScript access check'):
                await t.get('https://tass.ru/ekonomika/1')
    asyncio.run(scenario())


def test_imported_session_restores_cookies_and_user_agent(tmp_path):
    state = tmp_path / 'state.json'
    state.write_text(json.dumps({'cookies': [{'name': 'visitor', 'value': 'verified',
                                           'domain': 'tass.ru', 'path': '/'}],
                                'origins': [], '_news_parser': {'user_agent': 'VerifiedBrowser/1'}}))

    async def scenario():
        def handler(request):
            assert request.headers['User-Agent'] == 'VerifiedBrowser/1'
            assert 'visitor=verified' in request.headers.get('Cookie', '')
            return httpx.Response(200, text='actual document')
        async with Transport(Settings(browser_state=str(state)), client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler))) as t:
            assert await t.get('https://tass.ru/ekonomika/1') == 'actual document'
    asyncio.run(scenario())


@pytest.mark.browser
def test_403_js_check_can_navigate_to_200_article_and_persist_cookies(tmp_path, monkeypatch):
    for key in ('HTTPS_PROXY', 'HTTP_PROXY', 'ALL_PROXY', 'https_proxy', 'http_proxy', 'all_proxy'):
        monkeypatch.delenv(key, raising=False)
    state = tmp_path / 'visitor.json'
    body = 'Компания опубликовала финансовые результаты и увеличила инвестиции в развитие производства.'
    visited = []

    async def scenario():
        def handler(request):
            assert 'verified=1' in request.headers.get('Cookie', '')
            return httpx.Response(200, text='cookie-reused')
        async with Transport(Settings(browser_timeout=5, browser_state=str(state), delay=0),
                             client=httpx.AsyncClient(transport=httpx.MockTransport(handler))) as t:
            await t._ensure_browser()
            async def provider(route):
                u = route.request.url
                if not browser_request_allowed(u, route.request.resource_type):
                    await route.abort()
                    return
                visited.append(u)
                if 'servicepipe.tech/check.js' in u:
                    text = "setTimeout(()=>{document.cookie='verified=1; path=/';location.href='/ekonomika/1?Sig=AbC%2B'},250)"
                    await route.fulfill(status=200, content_type='application/javascript', body=text)
                elif '/ekonomika/1' in u:
                    await route.fulfill(status=200, content_type='text/html; charset=utf-8', body=(
                        '<h1>Результаты</h1><article>' + body + '</article>'))
                else:
                    await route.fulfill(status=403, content_type='text/html; charset=utf-8', body=(
                        '<js-challenge-loader></js-challenge-loader><script src="https://servicepipe.tech/check.js"></script>'))
            await t._context.route('**/*', provider)
            raw = await t.render('https://tass.ru/ekonomika/0?Sig=AbC%2B')
            assert body in raw
            assert await t.get('https://tass.ru/ekonomika/1') == 'cookie-reused'
        assert any('servicepipe.tech' in u for u in visited)
        assert visited[0].endswith('Sig=AbC%2B')
        assert json.loads(state.read_text())['cookies']
    asyncio.run(scenario())


@pytest.mark.browser
def test_unfinished_check_never_becomes_success(monkeypatch):
    for key in ('HTTPS_PROXY', 'HTTP_PROXY', 'ALL_PROXY', 'https_proxy', 'http_proxy', 'all_proxy'):
        monkeypatch.delenv(key, raising=False)

    async def scenario():
        async with Transport(Settings(browser_timeout=0.1, delay=0)) as t:
            await t._ensure_browser()
            await t._context.route('**/*', lambda route: route.fulfill(
                status=200, content_type='text/html', body='<js-challenge-loader></js-challenge-loader>'))
            with pytest.raises(FetchError, match='did not complete'):
                await t.render('https://tass.ru/ekonomika')
    asyncio.run(scenario())

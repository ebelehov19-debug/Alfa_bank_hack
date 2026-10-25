"""Interactive session setup uses scoped TLS and preserves both source sessions."""
import argparse
import asyncio
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from news_parser import session
from news_parser.transport import FetchError


def cookie(domain, value):
    return {'name': 'visitor', 'value': value, 'domain': domain, 'path': '/',
            'expires': -1, 'httpOnly': False, 'secure': True, 'sameSite': 'Lax'}


def initial_state():
    return {'cookies': [cookie('tass.ru', 'old-tass'), cookie('.alfabank.ru', 'old-alfa')],
            'origins': [{'origin': 'https://tass.ru', 'localStorage': [
                {'name': 'visitor', 'value': 'old-tass'}]},
                {'origin': 'https://alfabank.ru', 'localStorage': [
                    {'name': 'visitor', 'value': 'old-alfa'}]}],
            '_news_parser': {'user_agent': 'VerifiedBrowser/1'}}


def install_browser(monkeypatch, *, denied=False):
    """Fake headed browser: tests never open windows or use the network."""
    from playwright import async_api

    class Page:
        closed = False

        def __init__(self, context):
            self.context = context

        async def goto(self, url, **kwargs):
            self.url = url

        async def content(self):
            if denied:
                return '<js-challenge-loader></js-challenge-loader>'
            href = ('/news/t/release/a-release/' if self.context.alfa
                    else '/ekonomika/1')
            return f'<main><a href="{href}">Actual release title</a></main>'

        async def evaluate(self, expression):
            assert expression == 'navigator.userAgent'
            return 'InteractiveBrowser/2'

        async def close(self):
            self.closed = True

    class Context:
        closed = False

        def __init__(self, options):
            self.options = options
            self.alfa = options['ignore_https_errors']
            self.pages = []

        async def new_page(self):
            page = Page(self)
            self.pages.append(page)
            return page

        async def storage_state(self):
            # Include a stale foreign cookie to prove the merge only takes the
            # owning context's updates, even if browser state contains both.
            state = initial_state()
            index = int(self.alfa)
            value = 'new-alfa' if self.alfa else 'new-tass'
            state['cookies'][index]['value'] = value
            state['origins'][index]['localStorage'][0]['value'] = value
            return state

        async def close(self):
            self.closed = True

    class Browser:
        closed = False

        def __init__(self):
            self.contexts = []

        async def new_context(self, **options):
            context = Context(options)
            self.contexts.append(context)
            return context

        async def close(self):
            self.closed = True

    browser = Browser()

    class Chromium:
        async def launch(self, **options):
            browser.launch_options = options
            return browser

    class Playwright:
        chromium = Chromium()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    class Transport:
        def __init__(self, settings):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def _configure_context(self, context, *, alfa=False):
            context.route_scope = alfa

    async def confirm(*args):
        return ''

    for key in ('HTTPS_PROXY', 'HTTP_PROXY', 'ALL_PROXY'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(async_api, 'async_playwright', Playwright)
    monkeypatch.setattr(session, 'Transport', Transport)
    monkeypatch.setattr(session.asyncio, 'to_thread', confirm)
    return browser


@pytest.mark.parametrize('sources', [['tass', 'alfa'], ['alfa', 'tass'], ['alfa'], ['tass']])
def test_session_tls_is_separate_and_unopened_source_state_is_preserved(tmp_path, monkeypatch, sources):
    browser = install_browser(monkeypatch)
    state = tmp_path / 'visitor.json'
    before = initial_state()
    state.write_text(json.dumps(before), encoding='utf-8')
    args = argparse.Namespace(output=str(state), sources=sources, channel=None)

    asyncio.run(session.prepare(args))

    assert browser.launch_options == {'headless': False}
    assert len(browser.contexts) == len(sources)
    for source, context in zip(sources, browser.contexts):
        alfa = source == 'alfa'
        assert context.options['ignore_https_errors'] is alfa
        assert context.route_scope is alfa
        assert context.options['user_agent'] == 'VerifiedBrowser/1'
        loaded = context.options['storage_state']
        assert [item['value'] for item in loaded['cookies']] == [f'old-{source}']
        assert [item['origin'] for item in loaded['origins']] == [
            f'https://{"alfabank.ru" if alfa else "tass.ru"}']
        assert context.closed and all(page.closed for page in context.pages)
    saved = json.loads(state.read_text(encoding='utf-8'))
    values = {item['domain']: item['value'] for item in saved['cookies']}
    assert values == {'tass.ru': 'new-tass' if 'tass' in sources else 'old-tass',
                      '.alfabank.ru': 'new-alfa' if 'alfa' in sources else 'old-alfa'}
    origins = {item['origin']: item['localStorage'][0]['value'] for item in saved['origins']}
    assert origins == {'https://tass.ru': values['tass.ru'],
                       'https://alfabank.ru': values['.alfabank.ru']}
    assert saved['_news_parser']['user_agent'] == 'VerifiedBrowser/1'
    assert browser.closed


def test_new_session_reuses_first_browser_user_agent_for_other_context(tmp_path, monkeypatch):
    browser = install_browser(monkeypatch)
    state = tmp_path / 'visitor.json'
    args = argparse.Namespace(output=str(state), sources=['alfa', 'tass'], channel='msedge')

    asyncio.run(session.prepare(args))

    assert browser.launch_options['channel'] == 'msedge'
    assert 'user_agent' not in browser.contexts[0].options
    assert browser.contexts[1].options['user_agent'] == 'InteractiveBrowser/2'
    assert json.loads(state.read_text(encoding='utf-8'))['_news_parser'] == {
        'user_agent': 'InteractiveBrowser/2'}


def test_incomplete_challenge_preserves_saved_state_and_closes_browser(tmp_path, monkeypatch):
    browser = install_browser(monkeypatch, denied=True)
    state = tmp_path / 'visitor.json'
    before = initial_state()
    state.write_text(json.dumps(before), encoding='utf-8')
    original = state.read_bytes()
    args = argparse.Namespace(output=str(state), sources=['alfa'], channel=None)

    with pytest.raises(FetchError, match='session was not saved'):
        asyncio.run(session.prepare(args))

    assert state.read_bytes() == original
    assert browser.closed
    assert browser.contexts[0].closed and browser.contexts[0].pages[0].closed

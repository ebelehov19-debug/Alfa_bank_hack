import asyncio
from datetime import datetime, timedelta
import json
from pathlib import Path
import sys

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from news_parser.__main__ import collect
from news_parser.extract import (article_from_html, clean_text, listing_candidates,
                                 next_page, rss_candidates)
from news_parser.models import (Article, Candidate, Settings, UTC, canonical_url,
                                news_id, parse_date)
from news_parser.sources import EDisclosure, Moex, Source, SourceResult, Tass
from news_parser.storage import Storage, atomic_json
from news_parser.transport import FetchError, Transport

NOW = datetime(2026, 10, 22, 18, 0, tzinfo=UTC)
BODY = "Компания опубликовала результаты: выручка выросла, инвестиционная программа расширена."


def run(coro):
    return asyncio.run(coro)


def table(columns, rows):
    return {'columns': columns, 'data': rows}


def article(i=1, source='moex', date=NOW):
    url = f'https://moex.com/n{i}' if source == 'moex' else f'https://tass.ru/ekonomika/{i}'
    return Article(news_id(source, url), source, url, f'Тест {i}', BODY, date, NOW)


@pytest.mark.parametrize('value,expected', [
    ('2026-10-22T21:00:00+03:00', NOW),
    ('2026-10-22 21:00:00', NOW),
    ('Thu, 22 Oct 2026 21:00:00 +0300', NOW),
    ('22.10.2026 21:00', NOW),
    ('Опубликовано: 22.10.2026 21:00:00 мск', NOW),
    ('2026-10-22T18:00:00Z', NOW),
    ('2026-10-22', None), ('31.02.2026 12:00', None),
    ('19:00', None), ('нет даты', None), (None, None),
])
def test_dates(value, expected):
    assert parse_date(value) == expected


@pytest.mark.parametrize('url', ['http://127.0.0.1/x', 'https://tass.ru.evil.com/x',
                               'file:///etc/passwd', 'https://user:pass@tass.ru/x',
                               'https://tass.ru:8080/x'])
def test_untrusted_urls_rejected(url):
    with pytest.raises(ValueError):
        canonical_url(url)


def test_canonical_ids_preserve_event_value_and_ignore_tracking():
    a = 'https://www.e-disclosure.ru/portal/event.aspx?EventId=aBc-B-B&utm_source=x#top'
    b = 'http://e-disclosure.ru/portal/event.aspx?eventid=aBc-B-B'
    assert news_id('e_disclosure', a) == news_id('e_disclosure', b)
    assert news_id('e_disclosure', a) != news_id('e_disclosure', b.replace('aBc', 'abc'))
    assert news_id('moex', 'https://moex.com/ru/n42') == news_id('moex', 'https://www.moex.com/n42')


def test_clean_text_keeps_inline_words_and_paragraphs():
    assert clean_text('<p>Рост <b>выручки</b> на 10%.</p><p>Второй абзац.</p><script>x()</script>') == 'Рост выручки на 10%.\nВторой абзац.'


def test_rss_economy_only_and_not_teaser_as_full_body():
    raw = '''<rss version="2.0"><channel><title>Test</title>
    <item><title>Тест</title><link>https://tass.ru/ekonomika/1</link>
    <pubDate>Thu, 22 Oct 2026 21:00:00 +0300</pubDate><description>Короткий анонс</description>
    <category>Экономика и бизнес</category></item>
    <item><title>Спорт</title><link>https://tass.ru/sport/2</link></item>
    </channel></rss>'''
    candidates = rss_candidates(raw, 'tass')
    assert len(candidates) == 1
    assert candidates[0].text == ''
    assert candidates[0].published_at == NOW
    with pytest.raises(ValueError):
        rss_candidates('<html>Forbidden</html>', 'tass')


def test_jsonld_graph_and_published_instead_of_modified():
    raw = '<script type="application/ld+json">' + json.dumps({'@graph': [
        {'@type': 'Organization', 'name': 'Издатель'},
        {'@type': 'NewsArticle', 'headline': 'Результаты', 'articleBody': BODY,
         'datePublished': '2026-10-22T18:00:00Z', 'dateModified': '2026-10-22T19:00:00Z',
         'articleSection': 'Экономика'}]}) + '</script>'
    result = article_from_html(raw, 'tass', Candidate('https://tass.ru/ekonomika/1'))
    assert result.text == BODY
    assert result.published_at == NOW
    assert result.categories == ['Экономика']


def test_challenge_html_is_not_news_even_with_rss_metadata():
    raw = '<title>Forbidden</title><article>Verify you are human. ' + BODY + '</article>'
    with pytest.raises(ValueError):
        article_from_html(raw, 'tass', Candidate('https://tass.ru/ekonomika/1', 'Тест', NOW))


def test_disclosure_does_not_use_signature_or_event_date_as_publication():
    raw = f'''<div id="cont_wrap"><h2>ПАО Тест</h2><h4>Решения совета директоров</h4>
    <div>22.10.2026 21:00</div><a href="/portal/company.aspx?id=1">ПАО Тест</a>
    <div class="disclosure">1. Общие сведения<p>{BODY}</p><p>Дата сделки 01.01.2020 00:00</p></div></div>'''
    result = article_from_html(raw, 'e_disclosure', Candidate('https://e-disclosure.ru/portal/event.aspx?EventId=ABC'))
    assert result.published_at == NOW
    assert result.issuer == 'ПАО Тест'
    assert BODY in result.text
    raw = raw.replace('<div>22.10.2026 21:00</div>', '')
    assert article_from_html(raw, 'e_disclosure', Candidate('https://e-disclosure.ru/portal/event.aspx?EventId=ABC')).published_at is None


def test_listing_domains_dedup_pagination():
    raw = '''<a href="/ekonomika/1">Экономика</a><a href="/sport/2">Спорт</a>
    <a href="https://evil.test/ekonomika/3">Внешняя ссылка</a>
    <a href="/ekonomika/1">Повтор</a><a rel="next" href="?page=2">Далее</a>'''
    found = listing_candidates(raw, 'https://tass.ru/ekonomika', 'tass')
    assert [c.url for c in found] == ['https://tass.ru/ekonomika/1']
    assert next_page(raw, 'https://tass.ru/ekonomika') == 'https://tass.ru/ekonomika?page=2'


class MemoryTransport:
    def __init__(self, responses=None):
        self.responses = responses or {}
        self.urls = []
        self.renders = []

    async def get(self, url):
        self.urls.append(url)
        response = self.responses.get(url)
        if response is None or isinstance(response, Exception):
            raise FetchError('Unavailable')
        return response

    async def render(self, url):
        self.renders.append(url)
        return '<h1>Результаты</h1><time datetime="2026-10-22T18:00:00Z"></time><article>' + BODY + '</article>'

    async def listing(self, url, source, target, cutoff):
        return ['<a href="https://tass.ru/ekonomika/1">Результаты</a>']


class SyntheticSource(Source):
    name = 'moex'

    def __init__(self, candidates, settings=None, failures=()):
        super().__init__(MemoryTransport(), settings or Settings(browser=False))
        self.candidates = candidates
        self.failures = failures

    async def discover(self, cutoff, now, result, target):
        result.discovery_ok = True
        return self.candidates

    async def hydrate(self, candidate, result):
        if candidate.url in self.failures:
            raise FetchError('Temporary failure')
        return candidate


def test_window_inclusive_future_and_unknown_dates_rejected():
    dates = [NOW - timedelta(hours=48), NOW, NOW - timedelta(hours=48, seconds=1),
             NOW + timedelta(seconds=1), None]
    source = SyntheticSource([Candidate(f'https://moex.com/n{i}', 'Тест', d, text=BODY)
                              for i, d in enumerate(dates)])
    r = run(source.collect(NOW - timedelta(hours=48), NOW, set()))
    assert len(r.articles) == 2
    assert r.outside_window == 2 and r.invalid == 1


def test_limit_200_and_newest_order_with_duplicates_and_seen():
    candidates = [Candidate(f'https://moex.com/n{i}', 'Тест', NOW - timedelta(minutes=i), text=BODY)
                  for i in reversed(range(240))]
    candidates += [candidates[-1]]
    r = run(SyntheticSource(candidates).collect(NOW - timedelta(hours=48), NOW,
                                               {news_id('moex', 'https://moex.com/n0')}))
    assert len(r.articles) == 200
    assert r.articles[0].id == news_id('moex', 'https://moex.com/n1')
    assert r.articles[-1].id == news_id('moex', 'https://moex.com/n200')
    assert len({a.id for a in r.articles}) == 200


def test_failed_full_text_can_retry_next_run(tmp_path):
    c = Candidate('https://moex.com/n1', 'Тест', NOW, text=BODY)
    r = run(SyntheticSource([c], failures=[c.url]).collect(NOW - timedelta(hours=48), NOW, set()))
    assert not r.articles and r.status == 'partial'
    r = run(SyntheticSource([c]).collect(NOW - timedelta(hours=48), NOW, set()))
    assert len(r.articles) == 1


def test_moex_api_paginates_and_fetches_body():
    first = {'sitenews': table(['id', 'title', 'published_at'], [[1, 'Один', '2026-10-22 21:00:00']])}
    second = {'sitenews': table(['id', 'title', 'published_at'], [[2, 'Два', '2026-10-22 20:00:00'], [3, 'Старое', '2026-10-01 12:00:00']])}
    responses = {
        'https://iss.moex.com/iss/sitenews.json?start=0&iss.meta=off': json.dumps(first),
        'https://iss.moex.com/iss/sitenews.json?start=1&iss.meta=off': json.dumps(second),
    }
    for i in (1, 2):
        responses[f'https://iss.moex.com/iss/sitenews/{i}.json?iss.meta=off'] = json.dumps({
            'content': table(['id', 'title', 'published_at', 'body'],
                             [[i, f'Тест {i}', '2026-10-22 21:00:00', f'<p>{BODY}</p>']])})
    t = MemoryTransport(responses)
    r = run(Moex(t, Settings(browser=False)).collect(NOW - timedelta(hours=48), NOW, set()))
    assert len(r.articles) == 2 and r.status == 'ok'
    assert not t.renders
    assert r.articles[0].text == BODY


def test_moex_api_failure_uses_rss_then_http():
    rss = '<rss version="2.0"><channel><item><title>Тест</title><link>https://moex.com/n1</link><pubDate>Thu, 22 Oct 2026 21:00:00 +0300</pubDate></item></channel></rss>'
    t = MemoryTransport({'https://www.moex.com/export/news.aspx?cat=100': rss,
                         'https://www.moex.com/ru/news/': '<a href="/n1">Тест</a>'})
    r = run(Moex(t, Settings(limit=1)).collect(NOW - timedelta(hours=48), NOW, set()))
    assert len(r.articles) == 1
    assert 'rss' in r.methods and 'chromium_html' in r.methods
    assert r.warnings


def test_http_403_uses_browser_for_listing_and_full_text():
    t = MemoryTransport()
    r = run(Tass(t, Settings(limit=1)).collect(NOW - timedelta(hours=48), NOW, set()))
    assert len(r.articles) == 1
    assert t.renders == ['https://tass.ru/ekonomika/1']
    assert 'chromium_html' in r.methods


def test_disclosure_unavailable_is_error_not_success_empty():
    r = run(EDisclosure(MemoryTransport(), Settings(browser=False)).collect(
        NOW - timedelta(hours=48), NOW, set()))
    assert r.status == 'error' and r.errors


def test_latest_only_state_and_expiry(tmp_path):
    s = Storage(tmp_path)
    a = article()
    with s.lock():
        s.commit([a], {a.id: NOW.isoformat(), 'old': (NOW - timedelta(hours=49)).isoformat()}, {'run_id': 'one'})
    with s.lock():
        state = s.state(NOW - timedelta(hours=48))
        assert set(state) == {a.id}
        r = run(SyntheticSource([Candidate(a.url, a.title, NOW, text=BODY)]).collect(
            NOW - timedelta(hours=48), NOW, set(state)))
        assert not r.articles and r.already_seen == 1
        s.commit(r.articles, state, {'run_id': 'two'})
    assert json.loads((tmp_path / 'news.json').read_text()) == []
    assert json.loads((tmp_path / 'moex.json').read_text()) == []
    assert 'text' not in json.loads((tmp_path / 'state.json').read_text())


def test_partial_write_recovery_and_concurrent_lock(tmp_path, monkeypatch):
    import news_parser.storage as module
    s = Storage(tmp_path)
    original = module.atomic_json
    calls = 0

    def fail_third(path, value):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError('Simulated disk interruption')
        original(path, value)

    with s.lock():
        with pytest.raises(RuntimeError):
            with Storage(tmp_path).lock():
                pass
        monkeypatch.setattr(module, 'atomic_json', fail_third)
        with pytest.raises(OSError):
            s.commit([article()], {}, {'run_id': 'recover'})
    monkeypatch.setattr(module, 'atomic_json', original)
    with s.lock():
        assert not (tmp_path / '.pending.json').exists()
        assert len(json.loads((tmp_path / 'news.json').read_text())) == 1
        assert json.loads((tmp_path / 'run_report.json').read_text())['run_id'] == 'recover'


def test_corrupt_state_is_not_silently_reset(tmp_path):
    atomic_json(tmp_path / 'state.json', {'schema_version': 99, 'seen': {}})
    with pytest.raises(ValueError):
        Storage(tmp_path).state(NOW - timedelta(hours=48))


def test_http_retry_and_redirect_allowlist():
    async def scenario():
        requests = []
        async def handler(request):
            requests.append(request)
            if request.url.path == '/retry' and len(requests) == 1:
                return httpx.Response(503)
            if request.url.path == '/redirect':
                return httpx.Response(302, headers={'location': 'http://127.0.0.1/secret'})
            return httpx.Response(200, text='ok')
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        async with Transport(Settings(delay=0, retries=1), client=client) as t:
            assert await t.get('https://tass.ru/retry') == 'ok'
            assert len(requests) == 2
            with pytest.raises(ValueError):
                await t.get('https://tass.ru/redirect')
    run(scenario())


@pytest.mark.parametrize('hours,limit', [(49, 200), (48, 201), (0, 200), (48, 0)])
def test_hard_limits(hours, limit):
    with pytest.raises(ValueError):
        Settings(hours=hours, limit=limit)


@pytest.mark.browser
def test_real_chromium_fallback_with_local_intercepted_documents(monkeypatch):
    for name in ('HTTPS_PROXY', 'HTTP_PROXY', 'ALL_PROXY', 'https_proxy', 'http_proxy', 'all_proxy'):
        monkeypatch.delenv(name, raising=False)

    async def scenario():
        async with Transport(Settings(timeout=5, delay=0, max_pages=3)) as t:
            await t._ensure_browser()
            async def handle(route):
                if '/ekonomika/1' in route.request.url:
                    raw = '<h1>Тест</h1><time datetime="2026-10-22T18:00:00Z"></time><article>' + BODY + '</article>'
                else:
                    raw = '''<a href="/ekonomika/1">Тест</a><button onclick="const a=document.createElement('a');a.href='/ekonomika/2';a.textContent='Вторая';this.replaceWith(a)">Показать еще</button>'''
                await route.fulfill(status=200, content_type='text/html; charset=utf-8', body=raw)
            await t._context.route('**/*', handle)
            raw = await t.render('https://tass.ru/ekonomika/1')
            assert article_from_html(raw, 'tass', Candidate('https://tass.ru/ekonomika/1')).text == BODY
            pages = await t.listing('https://tass.ru/ekonomika', 'tass', 2, NOW - timedelta(hours=48))
            urls = {c.url for p in pages for c in listing_candidates(p, 'https://tass.ru/ekonomika', 'tass')}
            assert len(urls) == 2
    run(scenario())


@pytest.mark.browser
def test_disclosure_browser_date_modal_search_and_numeric_pagination(monkeypatch):
    for name in ('HTTPS_PROXY', 'HTTP_PROXY', 'ALL_PROXY', 'https_proxy', 'http_proxy', 'all_proxy'):
        monkeypatch.delenv(name, raising=False)
    raw = '''<meta charset="utf-8">
    <button onclick="document.getElementById('period').hidden=false">Выбрать дату публикации</button>
    <div id="period" hidden><input id="dateFrom"><input id="dateTo">
    <button onclick="this.parentNode.hidden=true">Подтвердить выбор</button></div>
    <button onclick="if(document.getElementById('dateFrom').value && document.getElementById('dateTo').value) show(1)">Искать</button>
    <div id="results"></div>
    <script>
    function show(n) {
      const a=document.createElement('a');
      a.href='/portal/event.aspx?EventId='+n;
      a.textContent='Решения совета директоров';
      document.getElementById('results').replaceChildren(a);
      if(n===1) {
        const nav=document.createElement('div');nav.className='pagination';
        const next=document.createElement('a');next.textContent='2';next.href='#';
        next.onclick=(e)=>{e.preventDefault();show(2)};nav.append(next);
        document.getElementById('results').append(nav);
      }
    }
    </script>'''

    async def scenario():
        async with Transport(Settings(timeout=5, delay=0, max_pages=3)) as t:
            await t._ensure_browser()
            await t._context.route('**/*', lambda route: route.fulfill(
                status=200, content_type='text/html; charset=utf-8', body=raw))
            pages = await t.listing('https://e-disclosure.ru/poisk-po-soobshheniyam',
                                    'e_disclosure', 2, NOW - timedelta(hours=48))
            urls = {c.url for p in pages for c in listing_candidates(
                p, 'https://e-disclosure.ru/poisk-po-soobshheniyam', 'e_disclosure')}
            assert len(urls) == 2
    run(scenario())

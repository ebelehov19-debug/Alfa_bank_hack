import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from news_parser.extract import article_from_html
from news_parser.models import Candidate, Settings
from news_parser.sources import Tass
from news_parser.tass_extract import clean_tass_text
from news_parser.transport import FetchError, Transport

ROWS = json.loads((Path(__file__).parent / 'fixtures/tass_export.json').read_text())
NOW = datetime(2026, 10, 23, 0, 0, tzinfo=timezone.utc)
BODY = 'МОСКВА, 23 октября. /ТАСС/. Компания увеличила выручку на 15% и инвестиции до 7 млрд рублей.'


@pytest.mark.parametrize('row', ROWS, ids=lambda r: r['url'].rsplit('/', 1)[-1])
def test_real_export_keeps_factual_paragraphs_without_page_chrome(row):
    text = clean_tass_text(row['text'], title=row['title'], categories=row['categories'])
    assert text.startswith(('МОСКВА,', 'ВАШИНГТОН,', 'РИМ,', 'ГРОЗНЫЙ,'))
    assert text[-1] in '.»"'
    for label in ('ГлавнаяЭкономика', 'Редакция сайта ТАСС', 'Читайте ТАСС в', 'Читайте также', '©'):
        assert label not in text
    assert row['title'] not in text.splitlines()
    paragraphs = [p for p in row['text'].splitlines() if len(p) > 180 or
                  p.startswith('"') or '/ТАСС/.' in p or '/Корр.' in p]
    assert paragraphs
    for p in paragraphs:
        assert p in text


def test_dom_body_preserves_all_paragraphs_inline_links_and_section_headings():
    html = f'''<article><header><h1>Результаты компании</h1><p>Анонс</p>
    <time itemprop="datePublished" datetime="2026-10-23T03:00:00+03:00"></time></header>
    <figure><img><figcaption>Подпись к фото © Фотограф</figcaption></figure>
    <div class="news-content"><div class="text-block"><p>{BODY}</p></div>
    <div class="Related_block"><p>Читайте также</p><a href="/ekonomika/9">Чужая новость</a></div>
    <h2>Мнение экспертов</h2><div class="text-block"><p>«Рост <a href="/ekonomika/2">производства</a> составил 12%», — сообщил директор.</p></div>
    <div class="Tags_list"><a>Россия</a><a>Промышленность</a></div></div></article>'''
    a = article_from_html(html, 'tass', Candidate('https://tass.ru/ekonomika/1'))
    assert a.title == 'Результаты компании'
    assert a.published_at == NOW
    assert a.text == BODY + '\nМнение экспертов\n«Рост производства составил 12%», — сообщил директор.'


def test_short_article_read_also_does_not_delete_its_parent_body():
    html = f'<h1>Результаты</h1><article><div><p>{BODY}</p><div>Читайте также<a>Другой материал</a></div></div></article>'
    assert article_from_html(html, 'tass', Candidate('https://tass.ru/ekonomika/1')).text == BODY


def test_cleaner_preserves_timeline_times_and_unpunctuated_facts():
    text = BODY + '\n10:00\nРоссия увеличила экспорт'
    assert clean_tass_text(text) == text
    intro = 'Компания выпустила предварительный комментарий.\n' + BODY
    assert clean_tass_text(intro) == intro


def test_metadata_ignores_update_dates_and_related_article_nodes():
    data = {'@graph': [
        {'@type': 'NewsArticle', 'url': 'https://tass.ru/ekonomika/2', 'datePublished': '2020-01-01T00:00:00Z', 'articleBody': 'Чужая статья'},
        {'@type': 'NewsArticle', 'url': 'https://tass.ru/ekonomika/1', 'headline': 'Текущая статья', 'articleBody': BODY,
         'datePublished': NOW.isoformat(), 'dateModified': '2026-10-23T10:00:00Z'}]}
    raw = '<script type="application/ld+json">' + json.dumps(data) + '</script>'
    a = article_from_html(raw, 'tass', Candidate('https://tass.ru/ekonomika/1'))
    assert a.text == BODY and a.published_at == NOW
    raw = f'<h1>Результаты</h1><time itemprop="dateModified" datetime="{NOW.isoformat()}">Обновлено</time><article>{BODY}</article>'
    assert article_from_html(raw, 'tass', Candidate('https://tass.ru/ekonomika/1')).published_at is None


def test_publication_time_from_matching_client_state_not_other_news():
    data = {'news': [{'id': 2, 'publishedAt': '2020-01-01T00:00:00Z'},
                     {'id': 1, 'publishedAt': int(NOW.timestamp() * 1000)}]}
    raw = '<h1>Результаты</h1><script id="__NEXT_DATA__" type="application/json">' + json.dumps(data) + f'</script><article>{BODY}</article>'
    assert article_from_html(raw, 'tass', Candidate('https://tass.ru/ekonomika/1')).published_at == NOW


def test_missing_publication_time_has_url_and_reason_in_report():
    class FixtureTransport:
        async def get(self, url):
            return f'<h1>Результаты</h1><article>{BODY}</article>'
        async def render(self, url):
            raise AssertionError('Browser is disabled')
        async def listing(self, *args):
            raise AssertionError('Browser is disabled')

    class FixtureTass(Tass):
        async def discover(self, cutoff, now, result, target):
            result.discovery_ok = True
            return [Candidate('https://tass.ru/ekonomika/1', 'Результаты')]

    result = asyncio.run(FixtureTass(FixtureTransport(), Settings(browser=False)).collect(
        NOW - timedelta(hours=48), NOW, set()))
    assert result.invalid == 1 and not result.articles
    assert result.to_dict()['invalid_reasons'] == {'missing_published_at': 1}
    assert result.to_dict()['invalid_records'] == [
        {'url': 'https://tass.ru/ekonomika/1', 'reasons': ['missing_published_at']}]


def test_http_challenge_selects_browser_once_for_remaining_articles():
    class FixtureTransport:
        count = 0
        async def get(self, url):
            self.count += 1
            raise FetchError('JavaScript access check; browser required')
        async def render(self, url):
            raise AssertionError('TASS-specific renderer should be selected')
        async def tass_render(self, url):
            return f'<h1>Результаты</h1><article>{BODY}</article>'
        async def listing(self, *args):
            return []
    class FixtureTass(Tass):
        async def discover(self, cutoff, now, result, target):
            result.discovery_ok = True
            return [Candidate(f'https://tass.ru/ekonomika/{i}', 'Результаты', NOW) for i in range(3)]
    transport = FixtureTransport()
    result = asyncio.run(FixtureTass(transport, Settings(limit=3)).collect(NOW - timedelta(hours=48), NOW, set()))
    assert len(result.articles) == 3 and transport.count == 1


def test_snapshot_retries_navigating_error_instead_of_dropping_news():
    class Context:
        async def cookies(self, urls):
            return []
    class Page:
        count = 0
        url = 'https://tass.ru/ekonomika/1'
        async def content(self):
            self.count += 1
            if self.count == 1:
                raise RuntimeError('Page.content: Unable to retrieve content because the page is navigating and changing the content.')
            return f'<h1>Результаты</h1><article>{BODY}</article>'
        async def wait_for_timeout(self, ms):
            pass
    async def scenario():
        t = Transport(Settings(browser_timeout=2))
        t._context = Context()
        page = Page()
        try:
            raw = await t._tass_snapshot(page, page.url, article=True)
            assert BODY in raw and page.count == 3
        finally:
            await t.client.aclose()
    asyncio.run(scenario())


@pytest.mark.browser
def test_tass_browser_redirect_returns_clean_body(monkeypatch):
    for name in ('HTTPS_PROXY', 'HTTP_PROXY', 'ALL_PROXY', 'https_proxy', 'http_proxy', 'all_proxy'):
        monkeypatch.delenv(name, raising=False)
    async def scenario():
        async with Transport(Settings(timeout=5, browser_timeout=5, delay=0)) as t:
            await t._ensure_browser()
            async def route(r):
                if '/ekonomika/0' in r.request.url:
                    raw = '<js-challenge-loader></js-challenge-loader><script>setTimeout(()=>location.href="/ekonomika/1",100)</script>'
                else:
                    raw = f'<article><header><h1>Результаты</h1></header><p>{BODY}</p><div class="Tags_list">Россия</div></article>'
                await r.fulfill(status=200, content_type='text/html; charset=utf-8', body=raw)
            await t._context.route('**/*', route)
            raw = await t.tass_render('https://tass.ru/ekonomika/0')
            parsed = article_from_html(raw, 'tass', Candidate('https://tass.ru/ekonomika/1'))
            assert parsed.text == BODY
    asyncio.run(scenario())

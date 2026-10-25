"""Alfa HTML contracts and pagination, with network retrieval replaced only."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from bs4 import BeautifulSoup
import pytest

from news_parser.alfa_extract import (alfa_listing_candidates, alfa_next_page, alfa_url,
                                      extract_alfa, parse_alfa_date)
from news_parser.models import Candidate, Settings, news_id
from news_parser.sources import ADAPTERS, Alfa


UTC = timezone.utc
NOW = datetime(2026, 10, 24, 12, tzinfo=UTC)
BASE = Alfa.listing_url
FIXTURES = Path(__file__).parent / 'fixtures'
USER_HTML = (FIXTURES / 'alfa_release_user.html').read_text(encoding='utf-8')
USER_URL = BeautifulSoup(USER_HTML, 'html.parser').select_one('link[rel="canonical"]')['href']
BODY = 'Полный текст новости о развитии образовательных программ и цифровых технологий.\nВторой абзац с подробностями проекта.'


def url(number):
    return BASE + f'release-{number}/'


def detail(number, published=NOW, text=BODY, **overrides):
    article = dict(path=f'release-{number}', title=f'Новость {number}', description=text,
                   publishedDate=published.isoformat() if published else None,
                   publicationType={'code': 'release'}, footers=[{'description': 'Чужой подвал банка'}])
    article.update(overrides)
    state = {'fetchResults': [{'name': 'get-pressrelease-data', 'response': {
        'status': 'fulfilled', 'value': {'article': article}}}]}
    return '<script type="application/json" id="app_state">' + json.dumps(state, ensure_ascii=False) + '</script>'


def listing(numbers, next_url=None, buttons=False, day='23 октября 2026'):
    cards = ''.join(f'<article><p>{day}</p><a href="{url(i)}">Новость {i}</a></article>' for i in numbers)
    next_link = f'<a rel="next" href="{next_url}">Следующая</a>' if next_url else ''
    pager = '<nav class="pagination"><button>1</button><button>2</button></nav>' if buttons else ''
    return '<main>' + cards + next_link + pager + '</main>'


def test_real_user_html_preserves_exact_timestamp_and_four_news_paragraphs_only():
    article = extract_alfa(USER_HTML, Candidate(USER_URL))
    assert ADAPTERS['alfa'] is Alfa
    assert article.published_at == datetime(2026, 10, 23, 10, 49, 24, 453000, tzinfo=UTC)
    assert article.title.startswith('Альфа-Банк победил в премии')
    assert len(article.text.splitlines()) == 4
    assert article.text.startswith('Альфа-Банк стал победителем премии')
    assert article.text.endswith('корпоративной социальной ответственности.')
    assert '**' not in article.text
    for excluded in ('крупнейший частный банк', '35 лет', 'Главное про инвестиции', 'Рекомендуемая', 'Поделиться'):
        assert excluded not in article.text


def test_real_user_html_without_state_uses_no_h1_dom_and_header_time():
    soup = BeautifulSoup(USER_HTML, 'html.parser')
    soup.select_one('#app_state').decompose()
    article = extract_alfa(str(soup), Candidate(USER_URL, published_at=NOW))
    assert article.published_at == datetime(2026, 10, 23, 10, 49, tzinfo=UTC)
    assert len(article.text.splitlines()) == 4
    assert 'крупнейший частный банк' not in article.text
    assert 'Главное про инвестиции' not in article.text


@pytest.mark.parametrize('changes', [dict(path='other-release'), dict(publicationType={'code': 'articles'})])
def test_wrong_release_state_is_rejected(changes):
    with pytest.raises(ValueError):
        extract_alfa(detail(1, **changes), Candidate(url(1)))


def test_unrelated_fetch_result_cannot_supply_title_text_or_date():
    raw = detail(1).replace('get-pressrelease-data', 'recommendations')
    with pytest.raises(ValueError):
        extract_alfa(raw, Candidate(url(1), title='Card', published_at=NOW, text=BODY))


def test_header_date_is_not_borrowed_from_body_card_or_recommendations():
    raw = ('<meta property="og:title" content="Новость"><header><p>Новость</p></header>'
           '<div class="cfCfF8"><p>' + BODY + ' 23 октября 2026 г. в 13:49</p></div>'
           '<aside><time datetime="2026-10-23T10:00:00Z"></time></aside>')
    article = extract_alfa(raw, Candidate(url(1), published_at=NOW))
    assert article.published_at is None
    assert '23 октября 2026 г. в 13:49' in article.text


@pytest.mark.parametrize(('value', 'expected'), [
    ('23 октября 2026 г. в 13:49', '2026-10-23T10:49:00+00:00'),
    ('23 октября 2026 13:49:24', '2026-10-23T10:49:24+00:00'),
    ('2026-10-23T10:49:24.453Z', '2026-10-23T10:49:24.453000+00:00'),
    ('23 октября; опубликовано 24 октября 2026 в 12:00', '2026-10-24T09:00:00+00:00'),
    ('29 февраля 2024 г. в 12:00', '2024-02-29T09:00:00+00:00'),
    ('29 февраля 2026 г. в 12:00', None),
    ('23 октября 2026', None), ('23 октября в 13:49', None), ('2026-10-23', None),
])
def test_full_dates_require_year_and_time(value, expected):
    actual = parse_alfa_date(value)
    assert (actual.isoformat() if actual else None) == expected


def test_cards_are_coarse_hints_and_infer_previous_year_in_january():
    date = parse_alfa_date('31 декабря', card=True, now=datetime(2027, 1, 2, tzinfo=UTC))
    assert date == datetime(2026, 12, 30, 21, tzinfo=UTC)
    assert parse_alfa_date('2026-10-23', card=True, now=NOW) == datetime(2026, 10, 22, 21, tzinfo=UTC)


def test_listing_uses_only_release_links_and_each_cards_own_date():
    raw = listing([1, 2]) + '<a href="/news/t/release/release-1/?utm_source=copy">Duplicate</a>'
    raw += '<a href="https://evil.test/news/t/release/bad/">External</a><a href="/news/t/articles/no/">Other type</a>'
    items = alfa_listing_candidates(raw, BASE, now=NOW)
    assert [item.url for item in items] == [url(1), url(2)]
    assert all(item.published_at == datetime(2026, 10, 22, 21, tzinfo=UTC) for item in items)


def test_listing_card_structure_confirmed_by_official_links_list_component():
    # Official 63652/98375 chunks: LinksList places its date label and h3 inside a.
    raw = ('<ul class="aLBOHl"><li class="dLBOHl bLBOHl hLBOHl">'
           '<a class="dLBOHl gLBOHl" href="/news/t/release/first/">'
           '<span class="aIrO76">23 октября 2026</span>'
           '<h3 class="dLBOHl eLBOHl">Заголовок пресс-релиза</h3></a></li></ul>')
    item, = alfa_listing_candidates(raw, BASE + '?rp-offset=10&rp-limit=10', now=NOW)
    assert item.title == 'Заголовок пресс-релиза'
    assert item.published_at == datetime(2026, 10, 22, 21, tzinfo=UTC)
    assert item.url == BASE + 'first/'


@pytest.mark.parametrize('raw', ['<js-challenge-loader></js-challenge-loader>', '<h1>Forbidden</h1>'])
def test_access_challenges_are_never_news(raw):
    with pytest.raises(ValueError):
        extract_alfa(raw, Candidate(url(1)))
    with pytest.raises(ValueError):
        alfa_listing_candidates(raw, BASE, now=NOW)


def test_release_identity_ignores_tracking_and_trailing_slash():
    assert alfa_url(url(1).rstrip('/') + '?utm_source=feed', BASE) == url(1)
    article = extract_alfa(detail(1), Candidate(url(1).rstrip('/')))
    assert article.url == url(1)


@pytest.mark.parametrize(('raw', 'base', 'expected'), [
    ('<a rel="next" href="?PAGEN_1=2">Next</a>', BASE, BASE + '?PAGEN_1=2'),
    ('<a aria-label="2" href="?page=2">2</a>', BASE, BASE + '?page=2'),
    ('<nav><a href="?page=1">1</a><a href="?page=3">3</a><a href="?page=2">2</a></nav>', BASE, BASE + '?page=2'),
    ('<a rel="next" href="/news/t/release/page/3/">›</a>', BASE + 'page/2/', BASE + 'page/3/'),
    ('<a rel="next" aria-disabled="true" href="?page=2">Next</a>', BASE, None),
    ('<a rel="next" href="https://evil.test/">Next</a>', BASE, None),
])
def test_anchor_pagination_is_scoped_and_preserves_parameter_case(raw, base, expected):
    assert alfa_next_page(raw, base) == expected


class MemoryTransport:
    def __init__(self, pages, browser_pages=(), rendered=None):
        self.pages, self.browser_pages = pages, browser_pages
        self.rendered = rendered or {}
        self.calls, self.browser_yields, self.closed = [], 0, False

    async def get(self, address):
        self.calls.append(address)
        response = self.pages[address]
        if isinstance(response, Exception):
            raise response
        return response

    async def render(self, address):
        return self.rendered[address]

    async def alfa_pages(self, address, cutoff, now):
        try:
            for raw in self.browser_pages:
                self.browser_yields += 1
                yield raw
        finally:
            self.closed = True


def collect(transport, *, limit=200, browser=False, seen=(), max_pages=20):
    settings = Settings(limit=limit, browser=browser, max_pages=max_pages)
    return asyncio.run(Alfa(transport, settings).collect(NOW - timedelta(hours=48), NOW, set(seen)))


def test_two_html_pages_use_exact_seven_day_boundaries_ignore_seen_and_future():
    second = BASE + '?page=2'
    cutoff = NOW - timedelta(days=7)
    transport = MemoryTransport({BASE: listing([1, 2, 3], second, day='17 октября 2026'),
        second: listing([3, 4, 5], day='17 октября 2026'),
        url(1): detail(1, cutoff), url(2): detail(2, cutoff - timedelta(microseconds=1)),
        url(3): detail(3, NOW - timedelta(days=6)), url(4): detail(4, NOW + timedelta(microseconds=1))})
    result = collect(transport, seen=[news_id('alfa', url(5))])
    assert [a.url for a in result.articles] == [url(3), url(1)]
    assert result.outside_window == 2 and result.already_seen == 1
    assert result.discovered == 5 and transport.calls.count(url(3)) == 1
    assert result.status == 'ok'


def test_stop_at_wholly_older_page_without_requesting_details_or_next_page():
    transport = MemoryTransport({BASE: listing([1, 2], BASE + '?page=2', day='15 октября 2026')})
    result = collect(transport)
    assert result.status == 'ok' and result.articles == [] and result.outside_window == 2
    assert transport.calls == [BASE]


def test_missing_detail_date_never_uses_card_hint():
    transport = MemoryTransport({BASE: listing([1]), url(1):
        '<h1>Новость</h1><div class="news-content"><p>' + BODY + '</p></div>'})
    result = collect(transport)
    assert result.invalid == 1 and result.articles == [] and result.status == 'partial'


def test_chromium_button_pages_are_lazy_deduplicated_and_closed_at_limit():
    transport = MemoryTransport({BASE: listing([1], buttons=True).replace('<button>2', '<button aria-label="Страница 2">2'),
                                url(1): detail(1), url(2): detail(2)},
                                browser_pages=[listing([1]), listing([2]), listing([3])])
    result = collect(transport, browser=True, limit=2)
    assert len(result.articles) == 2 and result.discovered == 2
    assert transport.browser_yields == 2 and transport.closed
    assert transport.calls.count(url(1)) == 1


def test_http_challenge_falls_back_to_browser_listing_and_full_detail():
    challenge = '<js-challenge-loader></js-challenge-loader>'
    transport = MemoryTransport({BASE: challenge, url(1): challenge},
                                browser_pages=[listing([1])], rendered={url(1): detail(1)})
    result = collect(transport, browser=True)
    assert len(result.articles) == 1 and result.status == 'ok'
    assert 'chromium_html' in result.methods and transport.closed


def test_200_cap_does_not_hydrate_or_visit_more_releases():
    transport = MemoryTransport({BASE: listing(range(201), BASE + '?page=2'),
                                **{url(i): detail(i) for i in range(200)}})
    result = collect(transport)
    assert len(result.articles) == 200
    assert len(transport.calls) == 201 and url(200) not in transport.calls


def test_looping_pagination_is_reported_and_bounded():
    second = BASE + '?page=2'
    transport = MemoryTransport({BASE: listing([1], second), second: listing([1], BASE), url(1): detail(1)})
    result = collect(transport)
    assert len(result.articles) == 1 and result.status == 'partial'
    assert any('repeated' in warning for warning in result.warnings)
    assert transport.calls.count(BASE) == 1

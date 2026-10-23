import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from news_parser.extract import article_from_html
from news_parser.models import Candidate, Settings
from news_parser.sources import Tass
from news_parser.tass_extract import parse_tass_display_date

URL = 'https://tass.ru/ekonomika/28243979'
OBSERVED = datetime.fromisoformat('2026-10-22T23:01:15+00:00')
BODY = 'МОСКВА, 22 октября. /ТАСС/. Компания увеличила выручку на 15% и инвестиции до 7 млрд рублей.'


def page(label):
    return (f'<main><article><h1>Результаты компании</h1><div class="news-author">'
            f'<span>Редакция сайта ТАСС</span><div class="publication">{label}</div>'
            f'</div><div class="news-content"><p>{BODY}</p></div></article></main>')


@pytest.mark.parametrize('label, expected', [
    ('00:28', '2026-10-22T21:28:00+00:00'),
    ('Сегодня, 00:28', '2026-10-22T21:28:00+00:00'),
    ('вчера, 21:52', '2026-10-22T18:52:00+00:00'),
    ('22 октября, 21:52 обновлено 22 октября, 23:01', '2026-10-22T18:52:00+00:00'),
    ('22 октября, 21:52обновлено 22 октября, 23:01', '2026-10-22T18:52:00+00:00'),
    ('22\u00a0октября, 21:52', '2026-10-22T18:52:00+00:00'),
    ('22 ОКТЯБРЯ , 21:52:05', '2026-10-22T18:52:05+00:00'),
    ('22 октября 2026, 21:52', '2026-10-22T18:52:00+00:00'),
    ('22 октября 2026 г., 21:52', '2026-10-22T18:52:00+00:00'),
    ('22 октября 2024, 21:52', '2024-10-22T18:52:00+00:00'),
])
def test_display_labels_use_moscow_time_and_first_publication(label, expected):
    assert parse_tass_display_date(label, observed_at=OBSERVED) == datetime.fromisoformat(expected)


@pytest.mark.parametrize('label', [
    '', '22 октября', 'обновлено 22 октября, 23:01', 'обновлено 00:28',
    '25:28', '00:60', '31 февраля, 21:52', '22 октября 2026, 99:00',
    'Компания выступила в 00:28', '22 октября 2026 года компания сообщила в 21:52',
    '23:59', 
])
def test_invalid_or_non_publication_labels_are_not_invented(label):
    assert parse_tass_display_date(label, observed_at=OBSERVED) is None


def test_year_boundary_and_explicit_year():
    now = datetime.fromisoformat('2027-01-01T00:05:00+03:00')
    assert parse_tass_display_date('31 декабря, 23:50', observed_at=now) == datetime.fromisoformat(
        '2026-12-31T20:50:00+00:00')
    assert parse_tass_display_date('1 января, 00:01', observed_at=now) == datetime.fromisoformat(
        '2026-12-31T21:01:00+00:00')
    assert parse_tass_display_date('31 декабря 2025, 23:50', observed_at=now).year == 2025


def test_leap_day_and_missing_timezone():
    now = datetime.fromisoformat('2024-03-01T00:05:00+03:00')
    assert parse_tass_display_date('29 февраля, 23:50', observed_at=now)
    assert parse_tass_display_date('00:28', observed_at=OBSERVED.replace(tzinfo=None)) is None


def test_fetch_spanning_midnight_rejects_relative_but_preserves_explicit_day():
    before = datetime.fromisoformat('2026-10-22T23:59:58+03:00')
    after = datetime.fromisoformat('2026-10-23T00:00:02+03:00')
    assert parse_tass_display_date('23:59', observed_at=after, started_at=before) is None
    assert parse_tass_display_date('Сегодня, 00:00', observed_at=after, started_at=before) is None
    assert parse_tass_display_date('22 октября, 23:59', observed_at=after, started_at=before) == datetime.fromisoformat(
        '2026-10-22T20:59:00+00:00')
    assert parse_tass_display_date('00:28', observed_at=OBSERVED + timedelta(hours=2),
                                  started_at=OBSERVED) == datetime.fromisoformat('2026-10-22T21:28:00+00:00')


@pytest.mark.parametrize('label', [
    '<span>22 октября, 21:52</span> <span>обновлено 22 октября, 23:01</span>',
    '<span>22 октября</span><span>, 21:52</span><span>обновлено</span><span>22 октября, 23:01</span>',
    '<span>22 октября, 21:52</span> <time itemprop="dateModified">23 октября, 00:28</time>',
])
def test_author_block_extracts_publication_without_update_or_page_chrome(label):
    a = article_from_html(page(label), 'tass', Candidate(URL), observed_at=OBSERVED)
    assert a.published_at == datetime.fromisoformat('2026-10-22T18:52:00+00:00')
    assert a.title == 'Результаты компании' and a.text == BODY


def test_time_only_author_label_and_explicit_publication_field():
    a = article_from_html(page('00:28'), 'tass', Candidate(URL), observed_at=OBSERVED)
    assert a.published_at == datetime.fromisoformat('2026-10-22T21:28:00+00:00')
    raw = f'<h1>Результаты</h1><time itemprop="datePublished">00:28</time><article><p>{BODY}</p></article>'
    assert article_from_html(raw, 'tass', Candidate(URL), observed_at=OBSERVED).published_at == a.published_at


def test_author_name_split_across_tags_and_whitespace():
    raw = page('00:28').replace('Редакция сайта ТАСС', 'Редакция\u00a0сайта <a>ТАСС</a>')
    assert article_from_html(raw, 'tass', Candidate(URL), observed_at=OBSERVED).published_at == datetime.fromisoformat(
        '2026-10-22T21:28:00+00:00')


def test_structured_publication_and_rss_take_priority_over_visible_label():
    rss_date = OBSERVED - timedelta(days=3)
    a = article_from_html(page('00:28'), 'tass', Candidate(URL, published_at=rss_date), observed_at=OBSERVED)
    assert a.published_at == rss_date
    raw = '<meta property="article:published_time" content="2026-10-22T18:00:05Z">' + page('00:28')
    assert article_from_html(raw, 'tass', Candidate(URL), observed_at=OBSERVED).published_at == datetime.fromisoformat(
        '2026-10-22T18:00:05+00:00')


@pytest.mark.parametrize('raw', [
    page('обновлено 00:28'),
    page('<time itemprop="dateModified">00:28</time>'),
    page('<span class="NewsHeader_updated">00:28</span>'),
    f'<h1>Результаты</h1><article><p>{BODY}</p><p>Компания выступила в 00:28.</p></article>',
    '<h1>Результаты</h1><article><div itemprop="articleBody"><p>Редакция сайта ТАСС</p><p>00:28</p></div></article>',
    f'<h1>Результаты</h1><article><p>{BODY}</p></article><aside>' + page('00:28') + '</aside>',
    f'<h1>Результаты</h1><article><p>{BODY}</p></article><div class="related">' + page('00:28') + '</div>',
])
def test_update_body_and_related_news_never_supply_publication_date(raw):
    assert article_from_html(raw, 'tass', Candidate(URL), observed_at=OBSERVED).published_at is None


def test_ambiguous_author_blocks_are_not_guessed():
    raw = page('00:28') + page('00:29')
    assert article_from_html(raw, 'tass', Candidate(URL), observed_at=OBSERVED).published_at is None


def test_date_fallback_is_used_by_http_hydration_and_old_dates_stay_outside_window():
    from news_parser.models import MSK
    now = datetime.now(timezone.utc)
    recent = now - timedelta(minutes=10)
    old = now - timedelta(days=4)
    months = 'января февраля марта апреля мая июня июля августа сентября октября ноября декабря'.split()
    def display(value):
        value = value.astimezone(MSK)
        return f'{value.day} {months[value.month - 1]} {value.year}, {value:%H:%M:%S}'
    class FixtureTransport:
        async def get(self, url):
            return page(display(old if url.endswith('/2') else recent))
        async def render(self, url):
            raise AssertionError('Browser disabled')
        async def listing(self, *args):
            raise AssertionError('Discovery mocked')
    class FixtureTass(Tass):
        async def discover(self, cutoff, now, result, target):
            result.discovery_ok = True
            return [Candidate(URL), Candidate('https://tass.ru/ekonomika/2')]
    result = asyncio.run(FixtureTass(FixtureTransport(), Settings(browser=False)).collect(
        now - timedelta(hours=48), now, set()))
    assert result.status == 'ok' and result.invalid == 0
    assert len(result.articles) == 1 and result.outside_window == 1

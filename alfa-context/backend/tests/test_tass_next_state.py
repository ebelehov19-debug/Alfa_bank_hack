"""Regression: actual TASS HTML supplied by the user, article 28243979.

The fixture preserves the original author/date block, all article paragraphs
and the two scripts containing publication metadata; unrelated page chrome,
ads and embedded base64 images were removed to keep it small.
"""
import asyncio
from datetime import datetime, timedelta
import json
from pathlib import Path
import sys

from bs4 import BeautifulSoup
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from news_parser.extract import article_from_html
from news_parser.models import Candidate, Settings
from news_parser.sources import Tass

URL = 'https://tass.ru/ekonomika/28243979'
NOW = datetime.fromisoformat('2026-10-22T23:33:53+00:00')
PUBLISHED = datetime.fromisoformat('2026-10-22T18:52:15+00:00')
UPDATED = datetime.fromisoformat('2026-10-22T20:01:46+00:00')
RAW = (Path(__file__).parent / 'fixtures/tass_28243979.html').read_text(encoding='utf-8')
BODY = 'МОСКВА, 22 октября. /ТАСС/. Компания увеличила выручку на 15% и инвестиции до 7 млрд рублей.'


def queue(node, *, flight=False):
    props = {'id': 'NewsArticle_microdata', 'type': 'application/ld+json',
             'children': json.dumps(node, ensure_ascii=False)}
    if flight:
        record = '1f:' + json.dumps(['$', '$L2c', None, props], ensure_ascii=False) + '\n'
        middle = len(record) // 2
        return ''.join('<script>self.__next_f.push(' + json.dumps([1, part], ensure_ascii=False) + ')</script>'
                       for part in (record[:middle], record[middle:]))
    return '<script>(self.__next_s=self.__next_s||[]).push(' + json.dumps([0, props], ensure_ascii=False) + ')</script>'


def metadata(**changes):
    return dict({'@type': 'NewsArticle', 'mainEntityOfPage': {'@id': '/ekonomika/28243979'},
                 'datePublished': '2026-10-22T21:52:15+03:00',
                 'dateModified': '2026-10-22T23:01:46+03:00'}, **changes)


@pytest.mark.parametrize('keep', ['both', '__next_s', '__next_f'])
def test_actual_html_reads_exact_publication_from_each_queue(keep):
    soup = BeautifulSoup(RAW, 'html.parser')
    for script in list(soup.select('script')):
        if keep != 'both' and keep not in script.get_text():
            script.decompose()
    assert soup.select_one('[class*="PageDateLabel_published"]').get_text() == '18:52'
    assert not soup.select('script[type="application/ld+json"], time')
    article = article_from_html(str(soup), 'tass', Candidate(URL), observed_at=NOW)
    assert article.published_at == PUBLISHED and article.published_at != UPDATED
    assert article.title.startswith('Минэнерго: импорт топлива')
    paragraphs = [p.get_text(' ', strip=True) for p in soup.select('article p')]
    assert len(paragraphs) == 5
    assert article.text == '\n'.join(paragraphs)
    assert len(article.text) == 1672


@pytest.mark.parametrize('observed', [NOW, NOW + timedelta(days=4), NOW.replace(year=2027)])
def test_saved_article_keeps_original_day_and_year_independent_of_fetch_date(observed):
    assert article_from_html(RAW, 'tass', Candidate(URL), observed_at=observed).published_at == PUBLISHED


@pytest.mark.parametrize('flight', [False, True])
def test_escaped_queue_data_survives_unicode_and_split_flight_chunks(flight):
    node = metadata(headline='Компания «Альфа» заявила: "Рост — 15%"', articleBody=BODY)
    a = article_from_html(queue(node, flight=flight), 'tass', Candidate(URL), observed_at=NOW)
    assert a.published_at == PUBLISHED and a.text == BODY
    assert a.title == node['headline']


@pytest.mark.parametrize('flight', [False, True])
@pytest.mark.parametrize('entity', ['/ekonomika/2', 'https://tass.ru/ekonomika/2',
                                   'https://www.moex.com/ekonomika/28243979'])
def test_other_article_or_other_host_never_supply_publication(flight, entity):
    node = metadata(mainEntityOfPage={'@id': entity})
    assert article_from_html(queue(node, flight=flight), 'tass', Candidate(URL), observed_at=NOW).published_at is None


@pytest.mark.parametrize('flight', [False, True])
def test_update_only_data_and_unassociated_news_are_not_publication(flight):
    node = metadata()
    del node['datePublished']
    assert article_from_html(queue(node, flight=flight), 'tass', Candidate(URL), observed_at=NOW).published_at is None
    node = metadata()
    del node['mainEntityOfPage']
    assert article_from_html(queue(node, flight=flight), 'tass', Candidate(URL), observed_at=NOW).published_at is None


def test_malformed_queue_and_arbitrary_javascript_do_not_become_metadata():
    raw = '<script>self.__next_s.push([0, JSON.parse("bad")]);</script>'
    assert article_from_html(raw, 'tass', Candidate(URL), observed_at=NOW).published_at is None
    raw = '<script>self.__next_f.push(' + json.dumps([1, json.dumps(metadata())]) + ')</script>'
    assert article_from_html(raw, 'tass', Candidate(URL), observed_at=NOW).published_at is None


def test_queue_javascript_is_not_executed(tmp_path):
    marker = tmp_path / 'unexpected.txt'
    raw = '<script>self.__next_s.push(__import__("pathlib").Path(' + repr(str(marker)) + ').write_text("bad"))</script>'
    assert article_from_html(raw, 'tass', Candidate(URL), observed_at=NOW).published_at is None
    assert not marker.exists()


def test_server_display_label_alone_is_not_mistaken_for_moscow_time():
    soup = BeautifulSoup(RAW, 'html.parser')
    for script in soup.select('script'):
        script.decompose()
    now = datetime.fromisoformat('2026-10-22T20:30:00+00:00')
    assert article_from_html(str(soup), 'tass', Candidate(URL), observed_at=now).published_at is None
    soup.select_one('[class*="PageDateLabel_published"]').string = '22 октября, 21:52'
    parsed = article_from_html(str(soup), 'tass', Candidate(URL), observed_at=NOW, rendered=True)
    assert parsed.published_at == PUBLISHED.replace(second=0)


def test_actual_article_flows_through_collection_and_old_articles_are_excluded():
    class FixtureTransport:
        async def get(self, url):
            return RAW
        async def render(self, url):
            raise AssertionError('Complete HTTP metadata needs no browser')
        async def listing(self, *args):
            raise AssertionError('Discovery mocked')
    class FixtureTass(Tass):
        async def discover(self, cutoff, now, result, target):
            result.discovery_ok = True
            return [Candidate(URL)]
    source = FixtureTass(FixtureTransport(), Settings(browser=False))
    fresh = asyncio.run(source.collect(NOW - timedelta(hours=48), NOW, set()))
    assert fresh.status == 'ok' and fresh.invalid == 0 and len(fresh.articles) == 1
    assert fresh.articles[0].published_at == PUBLISHED
    later = NOW + timedelta(days=4)
    old = asyncio.run(source.collect(later - timedelta(hours=48), later, set()))
    assert old.outside_window == 1 and not old.articles and old.invalid == 0

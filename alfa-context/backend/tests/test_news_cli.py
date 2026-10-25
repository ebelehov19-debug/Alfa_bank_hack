import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import news_parser.__main__ as cli
from news_parser.check import validate
from news_parser.models import Article, Settings, SOURCES, news_id
from news_parser.sources import SourceResult
from news_parser.storage import atomic_json
from news_parser.transport import Transport

BODY = 'Тестовые сведения для проверки конвейера: компания раскрыла новую финансовую отчётность.'


def test_two_sources_write_compatible_json_then_only_new_batch(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    date = now.isoformat()

    def handler(request):
        path = request.url.path
        host = request.url.host
        if host == 'iss.moex.com':
            if path.endswith('/sitenews.json'):
                data = {'sitenews': {'columns': ['id', 'title', 'published_at'], 'data': [[1, 'Тест MOEX', date]]}}
            else:
                data = {'content': {'columns': ['id', 'title', 'published_at', 'body'], 'data': [[1, 'Тест MOEX', date, BODY]]}}
            return httpx.Response(200, json=data)
        if path == '/rss/v2.xml':
            return httpx.Response(200, text=f'<rss version="2.0"><channel><item><title>Тест ТАСС</title><link>https://tass.ru/ekonomika/1</link><pubDate>{now.strftime("%a, %d %b %Y %H:%M:%S +0000")}</pubDate></item></channel></rss>')
        if path == '/ekonomika':
            return httpx.Response(200, text=f'<article><time datetime="{date}"></time><a href="/ekonomika/1">Тест ТАСС</a></article>')
        raw = '<h1>Тест</h1><script type="application/ld+json">' + json.dumps({
            '@type': 'NewsArticle', 'headline': 'Тест', 'datePublished': date, 'articleBody': BODY}) + '</script>'
        return httpx.Response(200, text=raw)

    monkeypatch.setattr(cli, 'Transport', lambda settings: Transport(
        settings, client=httpx.AsyncClient(transport=httpx.MockTransport(handler))))
    args = argparse.Namespace(hours=48, limit=1, timeout=5, delay=0, retries=0,
                              max_pages=2, no_browser=True, output=tmp_path,
                              sources=['moex', 'tass'])
    code = asyncio.run(cli.run(args))
    assert code == 0
    assert validate(tmp_path)['count'] == 2
    original_ids = {a['id'] for a in json.loads((tmp_path / 'news.json').read_text())}
    asyncio.run(cli.run(args))
    assert validate(tmp_path)['count'] == 0
    state = json.loads((tmp_path / 'state.json').read_text())
    assert set(state['seen']) == original_ids
    assert all(isinstance(value, str) for value in state['seen'].values())
    (tmp_path / 'tass.json').write_text('[{}]')
    with pytest.raises(ValueError):
        validate(tmp_path)


def test_default_collection_uses_source_windows_and_retains_week_old_alfa_state(tmp_path, monkeypatch):
    observed = datetime.now(timezone.utc)
    retained = news_id('alfa', 'https://alfabank.ru/news/old-release/')
    retained_moex = news_id('moex', 'https://moex.com/n998')
    expired = news_id('moex', 'https://moex.com/n999')
    atomic_json(tmp_path / 'state.json', {'schema_version': 1, 'seen': {
        retained: (observed - timedelta(days=6)).isoformat(),
        retained_moex: (observed - timedelta(hours=18)).isoformat(),
        expired: (observed - timedelta(days=3)).isoformat(),
    }})
    calls = {}

    class OfflineTransport:
        def __init__(self, settings):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    class OfflineSource:
        def __init__(self, source):
            self.source = source

        async def collect(self, cutoff, now, seen):
            calls[self.source] = (now - cutoff, seen)
            url = {'moex': 'https://moex.com/n1', 'tass': 'https://tass.ru/ekonomika/1',
                   'alfa': 'https://alfabank.ru/news/new-release/'}[self.source]
            age = timedelta(days=6) if self.source == 'alfa' else timedelta(hours=1)
            article = Article(news_id(self.source, url), self.source, url, 'Test release', BODY,
                              now - age, now)
            return SourceResult(self.source, articles=[article], discovery_ok=True)

    monkeypatch.setattr(cli, 'Transport', OfflineTransport)
    monkeypatch.setattr(cli, 'ADAPTERS', {
        source: (lambda transport, settings, name=source: OfflineSource(name)) for source in SOURCES
    })
    report = asyncio.run(cli.collect_to_directory(Settings(hours=12, browser=False), tmp_path))
    assert calls == {'moex': (timedelta(hours=12), {retained_moex}), 'tass': (timedelta(hours=12), set()),
                     'alfa': (timedelta(days=7), {retained})}
    assert report['hours'] == 168 and report['count'] == 3
    assert {source: details['hours'] for source, details in report['sources'].items()} == {
        'moex': 12, 'tass': 12, 'alfa': 168}
    assert validate(tmp_path)['count'] == 3
    state = json.loads((tmp_path / 'state.json').read_text(encoding='utf-8'))['seen']
    assert retained in state and retained_moex in state and expired not in state
    rows = json.loads((tmp_path / 'news.json').read_text(encoding='utf-8'))
    next(row for row in rows if row['source'] == 'moex')['published_at'] = (observed - timedelta(hours=13)).isoformat()
    atomic_json(tmp_path / 'news.json', rows)
    with pytest.raises(ValueError, match='out-of-window'):
        validate(tmp_path)

import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import news_parser.__main__ as cli
from news_parser.check import validate
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

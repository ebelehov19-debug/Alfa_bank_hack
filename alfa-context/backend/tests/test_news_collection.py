"""Collection entry point, cancellation and hand-off to the AI file importer."""

import asyncio
import json

import pytest

import news_parser.__main__ as cli
from news_parser.runner import collect_news
from news_parser.models import SOURCES
from news_parser.storage import Storage


def test_collection_uses_default_sources_and_reuses_the_output_browser_session(tmp_path, monkeypatch):
    expected = {"count": 0, "sources": {
        source: {"status": "ok", "exported": 0} for source in SOURCES
    }}
    calls = []

    async def collect(settings, directory, selected=None):
        calls.append((settings, directory, selected))
        return expected

    monkeypatch.setattr(cli, "collect_to_directory", collect)
    assert collect_news(tmp_path) == expected
    settings, directory, selected = calls[0]
    assert directory == tmp_path and selected is None
    assert settings.browser is True
    assert settings.hours == 48 and settings.limit == 200
    assert settings.browser_state == str(tmp_path / ".browser-state.json")


def test_collection_timeout_preserves_latest_files_and_releases_parser_lock(tmp_path, monkeypatch):
    original = [{"id": "previous-batch"}]
    latest = tmp_path / "news.json"
    latest.write_text(json.dumps(original), encoding="utf-8")
    cancelled = []

    async def blocked(settings, selected, state, now, transport):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    monkeypatch.setattr(cli, "collect", blocked)
    with pytest.raises(TimeoutError):
        collect_news(tmp_path, timeout=0.02)
    assert cancelled == [True]
    assert json.loads(latest.read_text(encoding="utf-8")) == original
    with Storage(tmp_path).lock():
        assert not (tmp_path / ".pending.json").exists()

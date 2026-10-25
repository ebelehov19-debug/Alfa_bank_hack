"""Offline Alfa source contracts, persistence and JSON -> Gemma -> GLM tests."""
from datetime import datetime, timedelta, timezone
import asyncio
import json
from pathlib import Path
import sys

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from news_ai import Pipeline, Settings, Store
from news_ai.pipeline import _article
from news_parser.check import validate
from news_parser.models import Article, Settings as ParserSettings, SOURCES, news_id
from news_parser.storage import Storage, atomic_json


NOW = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)
COMPANY = {"company_id": "U_alfa_test", "features": {"MAINOKVED_NAME": "Производство"},
           "limitations": ["Условия кредитных договоров неизвестны."]}
CLASSIFICATION = {"relevant": True, "confidence": 0.9, "category": "Финансы",
                  "reason": "Публикация о банковских услугах для бизнеса."}
ANALYSIS = {"type": "neutral", "relevance": 35, "summary": "Опубликованы условия банковской услуги.",
            "connection": "Применимость услуги к компании требует проверки.",
            "impact": "Влияние зависит от действующих договоров компании.",
            "action": "Сверить опубликованные условия с действующим договором.",
            "facts": ["В пресс-релизе описана услуга для бизнеса."],
            "uncertainties": ["Договоры и применимость условий неизвестны."]}


def article(source="alfa", number=1, age=timedelta(hours=1)):
    urls = {"alfa": f"https://alfabank.ru/news/t/release/test-release-{number}/",
            "moex": f"https://moex.com/n{number}",
            "tass": f"https://tass.ru/ekonomika/{number}"}
    url = urls[source]
    return Article(news_id(source, url), source, url, f"Исходная публикация {number}",
                   "Банк опубликовал условия новой услуги для бизнеса.\n"
                   "Применимость условий к конкретной компании требует проверки договора.",
                   NOW - age, NOW, ["Пресс-релизы"] if source == "alfa" else ["Экономика"],
                   "Альфа-Банк" if source == "alfa" else None)


def report(articles, run_id="alfa-test"):
    settings = ParserSettings()
    return {"schema_version": 1, "run_id": run_id, "count": len(articles),
            "hours": 168, "limit_per_source": 200,
            "window_start": settings.cutoff(NOW, "alfa").isoformat(), "window_end": NOW.isoformat(),
            "sources": {source: {"status": "ok", "exported": sum(a.source == source for a in articles),
                                 "window_start": settings.cutoff(NOW, source).isoformat(),
                                 "window_end": NOW.isoformat()}
                        for source in SOURCES}}


@pytest.fixture
def provider():
    settings = Settings(api_key="offline-alfa-test", base_url="https://provider.invalid/v1", retries=0)
    requests = []

    def respond(request):
        assert request.method == "POST" and request.url == "https://provider.invalid/v1/chat/completions"
        payload = json.loads(request.content)
        requests.append(payload)
        data = json.loads(payload["messages"][1]["content"])["untrusted_data"]
        if "question" in data:
            assert payload["model"] == settings.glm_model
            value = {"answer": "Нужно проверить действующий договор компании."}
        elif payload["model"] == settings.gemma_model:
            value = CLASSIFICATION
        else:
            assert payload["model"] == settings.glm_model
            value = ANALYSIS
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(value, ensure_ascii=False)}}]})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        yield settings, client, requests


def test_alfa_file_and_archive_reach_both_models_with_original_text_and_provenance(tmp_path, provider):
    settings, client, requests = provider
    original = article(age=timedelta(days=6))
    raw = original.to_dict()
    directory = tmp_path / "news"
    storage = Storage(directory)
    with storage.lock():
        storage.commit([original], {original.id: raw["published_at"]}, report([original]))
    assert json.loads((directory / "alfa.json").read_text(encoding="utf-8")) == [raw]
    assert json.loads((directory / "news.json").read_text(encoding="utf-8")) == [raw]
    assert validate(directory)["count"] == 1

    # An empty subsequent collection must leave the week-old Alfa article available in its archive.
    with storage.lock():
        storage.commit([], {original.id: raw["published_at"]}, report([], "empty"))
    assert json.loads((directory / "news.json").read_text(encoding="utf-8")) == []
    store_path = tmp_path / "ai.sqlite"
    store = Store(store_path, now=lambda: NOW)
    result = Pipeline(settings, store, directory, client).run(COMPANY, "cfo")
    assert result["errors"] == []
    assert result["counts"]["imported"] == result["counts"]["analyzed"] == 1
    assert [request["model"] for request in requests] == [settings.gemma_model, settings.glm_model]
    for request in requests:
        data = json.loads(request["messages"][1]["content"])["untrusted_data"]
        for field, value in raw.items():
            assert data["article"][field] == value, field
    assert json.loads(requests[1]["messages"][1]["content"])["untrusted_data"]["company"] == COMPANY
    item, = store.feed(COMPANY["company_id"], "cfo")
    assert item["id"] == original.id and item["url"] == original.url
    assert item["source"] == "alfa" and item["bank_view"] is None

    # Both raw text and model caches survive reconnects; no paid call is needed for a rerun.
    restarted_store = Store(store_path, now=lambda: NOW)
    restarted = Pipeline(settings, restarted_store, directory, client)
    assert restarted_store.feed(COMPANY["company_id"], "cfo") == [item]
    assert restarted.run(COMPANY, "cfo")["calls"] == 0
    answer = restarted.question(COMPANY, "cfo", original.id, "Что нужно проверить?")
    assert answer["answer"] and answer["sources"][0]["url"] == original.url
    assert json.loads(requests[-1]["messages"][1]["content"])["untrusted_data"]["article"]["text"] == original.text


def test_user_html_runs_through_real_source_files_and_both_model_requests(tmp_path, provider, monkeypatch):
    import news_parser.__main__ as parser
    from bs4 import BeautifulSoup
    from news_parser.alfa_extract import extract_alfa
    from news_parser.models import Candidate
    from news_parser.sources import Alfa

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)

    monkeypatch.setattr(parser, 'datetime', FrozenDateTime)
    raw_html = (Path(__file__).parent / 'fixtures' / 'alfa_release_user.html').read_text(encoding='utf-8')
    url = BeautifulSoup(raw_html, 'html.parser').select_one('link[rel="canonical"]')['href']
    expected = extract_alfa(raw_html, Candidate(url))

    class Transport:
        def __init__(self, settings):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, address):
            if address == Alfa.listing_url:
                return f'<main><article><p>23 октября 2026</p><a href="{url}">Release</a></article></main>'
            assert address == url
            return raw_html

    monkeypatch.setattr(parser, 'Transport', Transport)
    directory = tmp_path / 'news'
    collected = asyncio.run(parser.collect_to_directory(ParserSettings(browser=False), directory, ['alfa']))
    assert collected['sources']['alfa']['status'] == 'ok'
    assert validate(directory)['count'] == 1
    saved, = json.loads((directory / 'alfa.json').read_text(encoding='utf-8'))
    assert saved['text'] == expected.text and saved['published_at'] == expected.published_at.isoformat()
    assert len(saved['text'].splitlines()) == 4
    settings, client, requests = provider
    result = Pipeline(settings, Store(tmp_path / 'ai.sqlite', now=lambda: NOW), directory, client).run(COMPANY, 'cfo')
    assert result['counts']['analyzed'] == 1 and result['errors'] == []
    assert len(requests) == 2
    for request in requests:
        article_data = json.loads(request['messages'][1]['content'])['untrusted_data']['article']
        assert {key: article_data[key] for key in saved} == saved
        assert article_data.get('text_truncated', False) is False


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("position", ["boundary", "expired", "future"])
def test_pipeline_enforces_week_for_alfa_and_48_hours_for_other_sources(tmp_path, provider, source, position):
    settings, client, requests = provider
    window = timedelta(days=7) if source == "alfa" else timedelta(hours=48)
    age = {"boundary": window, "expired": window + timedelta(seconds=1),
           "future": -timedelta(seconds=1)}[position]
    original = article(source, age=age)
    directory = tmp_path / "news"
    atomic_json(directory / "news.json", [original.to_dict()])
    store = Store(tmp_path / "ai.sqlite", now=lambda: NOW)
    result = Pipeline(settings, store, directory, client).run(COMPANY, "cfo")
    assert result["errors"] == [] and result["counts"]["imported"] == 1
    expected = int(position == "boundary")
    assert result["counts"]["analyzed"] == expected
    assert len(requests) == 2 * expected
    assert len(store.articles()) == 1  # Expired raw data remains durable.
    assert [item["id"] for item in store.feed(COMPANY["company_id"], "cfo")] == ([original.id] if expected else [])


def test_feed_and_questions_expire_using_each_sources_window(tmp_path, provider):
    settings, client, requests = provider
    clock = [NOW]
    originals = [article("alfa", age=timedelta(days=5)), article("moex", age=timedelta(hours=24)),
                 article("tass", age=timedelta(hours=24))]
    directory = tmp_path / "news"
    atomic_json(directory / "news.json", [a.to_dict() for a in originals])
    store = Store(tmp_path / "ai.sqlite", now=lambda: clock[0])
    pipeline = Pipeline(settings, store, directory, client)
    assert pipeline.run(COMPANY, "cfo")["counts"]["analyzed"] == 3
    clock[0] = NOW + timedelta(hours=25)
    assert [item["source"] for item in store.feed(COMPANY["company_id"], "cfo")] == ["alfa"]
    for original in originals[1:]:
        with pytest.raises(LookupError):
            pipeline.question(COMPANY, "cfo", original.id, "Что проверить?")
    assert pipeline.question(COMPANY, "cfo", originals[0].id, "Что проверить?")["answer"]
    clock[0] = NOW + timedelta(days=2, seconds=1)
    assert store.feed(COMPANY["company_id"], "cfo") == []
    before = len(requests)
    with pytest.raises(LookupError):
        pipeline.question(COMPANY, "cfo", originals[0].id, "Что проверить?")
    assert len(requests) == before


def test_import_accepts_full_600_article_batch_from_three_sources_without_models(tmp_path):
    directory = tmp_path / "news"
    rows = [article(source, number).to_dict() for source in SOURCES for number in range(200)]
    atomic_json(directory / "news.json", rows)
    store = Store(tmp_path / "ai.sqlite", now=lambda: NOW)
    result = Pipeline(Settings(api_key=""), store, directory).run(COMPANY, "cfo")
    assert result["counts"]["imported"] == 600 and len(store.articles()) == 600
    assert [error["code"] for error in result["errors"]] == ["missing_configuration"]
    assert result["calls"] == 0


@pytest.mark.parametrize("url", [
    "https://alfabank.ru.evil.example/news/release/",
    "https://user:password@alfabank.ru/news/release/",
    "https://alfabank.ru:8080/news/release/",
    "https://tass.ru/ekonomika/123",
])
def test_alfa_import_rejects_urls_outside_its_source(url):
    with pytest.raises(ValueError):
        _article({**article().to_dict(), "url": url})


def test_alfa_import_keeps_parser_fields_and_rejects_mismatched_source_id():
    raw = article().to_dict()
    assert _article(raw) == raw
    with pytest.raises(ValueError):
        _article({**raw, "id": "tass:" + raw["id"].split(":")[1]})


def test_state_pruning_preserves_six_day_alfa_ids_and_drops_stale_moex_tass(tmp_path):
    storage = Storage(tmp_path / "news")
    originals = [article(source, age=timedelta(days=6)) for source in SOURCES]
    expired = article("alfa", 2, timedelta(days=7, seconds=1))
    originals.append(expired)
    seen = {a.id: a.published_at.isoformat() for a in originals}
    atomic_json(storage.directory / "state.json", {"schema_version": 1, "seen": seen})
    settings = ParserSettings(hours=12)
    with storage.lock():
        state = storage.state({source: settings.cutoff(NOW, source) for source in SOURCES})
    assert state == {originals[2].id: originals[2].published_at.isoformat()}
    assert settings.cutoff(NOW, "alfa") == NOW - timedelta(days=7)
    assert settings.cutoff(NOW, "moex") == NOW - timedelta(hours=12)


def test_recovery_reads_pre_alfa_transaction_without_losing_existing_articles(tmp_path):
    storage = Storage(tmp_path / "news")
    original = article("moex")
    raw = original.to_dict()
    legacy_report = report([original], "legacy-two-sources")
    legacy_report["sources"].pop("alfa")
    legacy_report["hours"] = 48
    legacy_report["window_start"] = (NOW - timedelta(hours=48)).isoformat()
    atomic_json(storage.directory / ".pending.json", {
        "moex.json": [raw], "tass.json": [], "news.json": [raw],
        "state.json": {"schema_version": 1, "seen": {original.id: raw["published_at"]}},
        "run_report.json": legacy_report,
    })
    with storage.lock():
        assert not (storage.directory / ".pending.json").exists()
    assert json.loads((storage.directory / "alfa.json").read_text(encoding="utf-8")) == []
    assert json.loads((storage.directory / "news.json").read_text(encoding="utf-8")) == [raw]
    assert json.loads((storage.directory / "state.json").read_text(encoding="utf-8"))["seen"] == {original.id: raw["published_at"]}
    assert json.loads((storage.directory / "batches/legacy-two-sources.json").read_text(encoding="utf-8"))["articles"] == [raw]
    assert validate(storage.directory)["count"] == 1

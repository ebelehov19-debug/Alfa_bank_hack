"""Offline contract/integration tests: no RSS, browser, credentials or live models."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import sys

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from news_ai import Pipeline, Settings, Store
from news_ai import schema
from news_ai.config import DEFAULT_BASE_URL, DEFAULT_GEMMA_MODEL, DEFAULT_GLM_MODEL
from news_ai.store import digest
from news_parser.models import Article
from news_parser.storage import Storage, atomic_json

BODY = "Компания раскрыла результаты. Поставки выросли, но период, валюта и причинная связь с клиентом неизвестны."
NOW = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)
COMPANY = {"company_id": "U_1", "features": {"MAINOKVED_NAME": "Производство"},
           "limitations": ["Валюта и период платежей неизвестны."]}
CLASSIFICATION = {"relevant": True, "confidence": 0.9, "category": "Экономика", "reason": "Экономическая новость."}
ANALYSIS = {"type": "risk", "relevance": 75, "summary": "Поставки выросли.",
            "connection": "Прямая связь с компанией не подтверждена.",
            "impact": "Возможное влияние требует проверки.", "action": "Проверить условия закупок.",
            "facts": ["В новости сообщается о росте поставок."],
            "uncertainties": ["Валюта и период неизвестны."]}


def article(number=1, source="tass", text=BODY):
    return {"id": source + ":" + sha256(str(number).encode()).hexdigest()[:24],
            "source": source, "url": f"https://tass.ru/ekonomika/{number}" if source == "tass" else f"https://moex.com/n{number}",
            "title": f"Исходный заголовок {number}", "text": text,
            "published_at": f"2026-10-23T06:{number % 60:02d}:00+00:00", "collected_at": "2026-10-23T07:00:00+00:00",
            "categories": ["Экономика"], "issuer": None, "language": "ru"}


def write_batch(directory, rows):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "news.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")


def configured(**kwargs):
    return Settings(api_key="test-key-secret", base_url="https://provider.invalid/v1", retries=0, **kwargs)


class Provider:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.client = httpx.Client(transport=httpx.MockTransport(self.handle))

    def handle(self, request):
        assert request.url == "https://provider.invalid/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer test-key-secret"
        self.requests.append(json.loads(request.content))
        assert self.responses, "Unexpected paid request"
        response = self.responses.pop(0)
        if isinstance(response, httpx.Response):
            return response
        if isinstance(response, Exception):
            raise response
        content = response if isinstance(response, str) else json.dumps(response, ensure_ascii=False)
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def setup(tmp_path, responses, rows=None, settings=None):
    news = tmp_path / "news"
    write_batch(news, rows if rows is not None else [article()])
    provider = Provider(responses)
    store = Store(tmp_path / "ai.sqlite", now=lambda: NOW)
    return Pipeline(settings or configured(), store, news, provider.client), provider, store, news


def test_configuration_dotenv_override_defaults_and_secret_redaction(tmp_path, monkeypatch):
    for key in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_GEMMA_MODEL", "LLM_GLM_MODEL", "NEWS_AI_TOPIC", "NEWS_AI_BATCH_LIMIT"):
        monkeypatch.delenv(key, raising=False)
    assert Settings.from_env(tmp_path / "absent.env").missing() == ["LLM_API_KEY"]
    assert Settings().base_url == DEFAULT_BASE_URL
    env = tmp_path / ".env"
    env.write_text('# comment\nLLM_API_KEY="file-secret"\nLLM_BASE_URL=https://provider.invalid/v1\nNEWS_AI_TOPIC=\'Финансы\'\n', encoding="utf-8")
    monkeypatch.setenv("LLM_API_KEY", "env-secret")
    settings = Settings.from_env(env)
    assert settings.api_key == "env-secret"
    assert settings.topic == "Финансы"
    assert settings.public()["models"] == {"classifier": DEFAULT_GEMMA_MODEL, "analyst": DEFAULT_GLM_MODEL}
    public = json.dumps(settings.public())
    assert "env-secret" not in public and "file-secret" not in public and "api_key" not in public
    assert "env-secret" not in repr(settings)
    monkeypatch.setenv("NEWS_AI_BATCH_LIMIT", "51")
    with pytest.raises(ValueError):
        Settings.from_env(env)


@pytest.mark.parametrize("value", [
    '```json\n' + json.dumps(CLASSIFICATION) + '\n```',
    json.dumps({**CLASSIFICATION, "relevant": "true"}),
    json.dumps({**CLASSIFICATION, "confidence": True}),
    json.dumps({**CLASSIFICATION, "confidence": 1.1}),
    json.dumps({**CLASSIFICATION, "confidence": float("nan")}),
    json.dumps({**CLASSIFICATION, "unknown": "field"}),
    '{"relevant":true,"relevant":false,"confidence":0.8,"category":"x","reason":"x"}',
])
def test_strict_classification_rejects_malformed_or_ambiguous_json(value):
    with pytest.raises(schema.InvalidResponse):
        schema.classification(value)


@pytest.mark.parametrize("change", [{"relevance": 74.5}, {"relevance": True}, {"relevance": 101},
                                        {"type": "bank"}, {"type": []}, {"facts": "assertion"},
                                        {"summary": ""}, {"url": "https://invented.invalid"}])
def test_strict_analysis_rejects_bad_types_and_provenance_fields(change):
    with pytest.raises(schema.InvalidResponse):
        schema.analysis(json.dumps({**ANALYSIS, **change}))


def test_two_stage_pipeline_provenance_untrusted_inputs_and_durable_cache(tmp_path):
    pipeline, provider, store, news = setup(tmp_path, [CLASSIFICATION, ANALYSIS])
    result = pipeline.run(COMPANY, "cfo")
    assert result["counts"] == {"imported": 1, "classified": 1, "rejected": 0, "analyzed": 1, "cached": 0, "errors": 0}
    assert [request["model"] for request in provider.requests] == [DEFAULT_GEMMA_MODEL, DEFAULT_GLM_MODEL]
    payload = json.loads(provider.requests[1]["messages"][1]["content"])["untrusted_data"]
    assert payload["company"]["limitations"] == COMPANY["limitations"]
    assert COMPANY["limitations"][0] in payload["limitations"]
    assert payload["article"]["url"] == article()["url"]
    assert "банка" in provider.requests[1]["messages"][0]["content"]
    item = store.feed("U_1", "cfo")[0]
    assert item["title"] == article()["title"] and item["url"] == article()["url"]
    assert item["id"] == article()["id"] and item["bank_view"] is None
    assert "_content_hash" not in item
    assert store.feed("U_2", "cfo") == [] and store.feed("U_1", "ceo") == []
    reloaded = Store(tmp_path / "ai.sqlite", now=lambda: NOW)
    assert reloaded.feed("U_1", "cfo") == [item]
    write_batch(news, [])
    repeated = Pipeline(configured(), reloaded, news, provider.client).run(COMPANY, "cfo")
    assert repeated["calls"] == 0 and repeated["counts"]["cached"] == 1


def test_gemma_reject_skips_glm_and_rejection_is_cached(tmp_path):
    pipeline, provider, store, _ = setup(tmp_path, [{**CLASSIFICATION, "relevant": False}])
    result = pipeline.run(COMPANY, "cfo")
    assert result["counts"]["rejected"] == 1 and result["counts"]["analyzed"] == 0
    assert len(provider.requests) == 1 and store.feed("U_1", "cfo") == []
    assert pipeline.run(COMPANY, "cfo")["calls"] == 0


def test_failed_analysis_preserves_classification_and_raw_for_retry(tmp_path):
    pipeline, provider, store, news = setup(tmp_path, [CLASSIFICATION, "not JSON", ANALYSIS])
    first = pipeline.run(COMPANY, "cfo")
    assert first["errors"][0]["code"] == "invalid_response"
    assert store.feed("U_1", "cfo") == []
    write_batch(news, [])
    second = pipeline.run(COMPANY, "cfo")
    assert second["counts"]["classified"] == 0 and second["counts"]["analyzed"] == 1
    assert [request["model"] for request in provider.requests] == [DEFAULT_GEMMA_MODEL, DEFAULT_GLM_MODEL, DEFAULT_GLM_MODEL]


def test_cache_keys_include_company_role_topic_models_and_raw_content(tmp_path):
    pipeline, provider, store, news = setup(tmp_path, [CLASSIFICATION, ANALYSIS, ANALYSIS, ANALYSIS])
    pipeline.run(COMPANY, "cfo")
    pipeline.run(COMPANY, "ceo")
    changed_company = {**COMPANY, "features": {"MAINOKVED_NAME": "Торговля"}}
    pipeline.run(changed_company, "cfo")
    assert len(provider.requests) == 4  # Only company/role-dependent analysis is repeated.
    for settings, row in [(replace(configured(), topic="Новая тема"), article()),
                          (replace(configured(), glm_model="glm-new"), article()),
                          (configured(), article(text=BODY + " Изменённый текст."))]:
        provider.responses.extend([CLASSIFICATION, ANALYSIS])
        write_batch(news, [row])
        result = Pipeline(settings, store, news, provider.client).run(COMPANY, "cfo")
        assert result["counts"]["classified"] == 1 and result["counts"]["analyzed"] == 1


def test_budget_counts_retry_attempts_and_stops_before_glm(tmp_path, monkeypatch):
    monkeypatch.setattr("news_ai.client.time.sleep", lambda _: None)
    settings = replace(configured(), retries=1, max_calls_per_run=2)
    pipeline, provider, store, _ = setup(tmp_path, [httpx.Response(429), CLASSIFICATION], settings=settings)
    result = pipeline.run(COMPANY, "cfo")
    assert result["calls"] == 2 and len(provider.requests) == 2
    assert result["errors"][0]["code"] == "call_limit"
    assert store.feed("U_1", "cfo") == []


def test_shared_provider_budget_is_not_retried_or_exposed(tmp_path):
    settings = replace(configured(), retries=3)
    pipeline, provider, _, _ = setup(tmp_path, [httpx.Response(429, text="Budget has been exceeded: test-key-secret")], settings=settings)
    result = pipeline.run(COMPANY, "cfo")
    assert result["errors"][0]["code"] == "budget_exceeded"
    assert len(provider.requests) == 1
    assert "test-key-secret" not in json.dumps(result)


def test_missing_key_imports_raw_but_never_calls_provider(tmp_path):
    pipeline, provider, store, _ = setup(tmp_path, [], settings=replace(configured(), api_key=""))
    result = pipeline.run(COMPANY, "cfo")
    assert result["counts"]["imported"] == 1 and len(store.articles()) == 1
    assert result["errors"][0]["missing"] == ["LLM_API_KEY"]
    assert not provider.requests


def test_batch_limit_preserves_remaining_articles_and_feed_is_ranked(tmp_path):
    pipeline, provider, store, _ = setup(tmp_path, [CLASSIFICATION, {**ANALYSIS, "relevance": 20},
                                                  CLASSIFICATION, {**ANALYSIS, "relevance": 95}],
                                        rows=[article(1), article(2)], settings=configured(batch_limit=1))
    first = pipeline.run(COMPANY, "cfo")
    assert first["counts"]["analyzed"] == 1 and len(store.articles()) == 2
    second = pipeline.run(COMPANY, "cfo")
    assert second["counts"]["analyzed"] == 1 and second["counts"]["cached"] == 1
    assert [item["relevance"] for item in store.feed("U_1", "cfo")] == [95, 20]


def test_question_uses_saved_analysis_only_glm_and_original_sources(tmp_path):
    pipeline, provider, store, _ = setup(tmp_path, [CLASSIFICATION, ANALYSIS, {"answer": "Нужны данные о периоде."}])
    pipeline.run(COMPANY, "cfo")
    result = pipeline.question(COMPANY, "cfo", article()["id"], "Какой период?")
    assert result["answer"] == "Нужны данные о периоде."
    assert result["sources"][0]["url"] == article()["url"]
    assert provider.requests[-1]["model"] == DEFAULT_GLM_MODEL
    assert pipeline.question(COMPANY, "cfo", article()["id"], "Какой период?")["cached"] is True
    with pytest.raises(LookupError):
        pipeline.question(COMPANY, "ceo", article()["id"], "Какой период?")


def test_archive_precedes_latest_and_state_survives_empty_runs_and_imports(tmp_path, monkeypatch):
    import news_parser.storage as module
    directory = tmp_path / "news"
    storage = Storage(directory)
    raw = article()
    obj = Article(raw["id"], raw["source"], raw["url"], raw["title"], raw["text"],
                  datetime.fromisoformat(raw["published_at"]), datetime.fromisoformat(raw["collected_at"]))
    original = module.atomic_json

    def observe(path, value):
        if path.name in {"news.json", "state.json"}:
            assert list((directory / "batches").glob("*.json")), "Archive must precede latest/dedup writes"
        original(path, value)

    monkeypatch.setattr(module, "atomic_json", observe)
    with storage.lock():
        storage.commit([obj], {raw["id"]: raw["published_at"]}, {"run_id": "../unsafe-run"})
    with storage.lock():
        storage.commit([], {raw["id"]: raw["published_at"]}, {})
    archives = list((directory / "batches").glob("*.json"))
    assert len(archives) == 1 and archives[0].parent == directory / "batches"
    assert json.loads((directory / "news.json").read_text()) == []
    provider = Provider([CLASSIFICATION, ANALYSIS])
    store = Store(tmp_path / "ai.sqlite", now=lambda: NOW)
    result = Pipeline(configured(), store, directory, provider.client).run(COMPANY, "cfo")
    assert result["counts"]["imported"] == 1 and result["counts"]["analyzed"] == 1


def test_archive_failure_does_not_advance_latest_or_dedup_state(tmp_path, monkeypatch):
    import news_parser.storage as module
    storage = Storage(tmp_path / "news")
    raw = article()
    obj = Article(raw["id"], raw["source"], raw["url"], raw["title"], raw["text"],
                  datetime.fromisoformat(raw["published_at"]), datetime.fromisoformat(raw["collected_at"]))
    original = module.atomic_json

    def interrupt(path, value):
        if path.parent.name == "batches":
            raise OSError("Simulated disk full")
        original(path, value)

    monkeypatch.setattr(module, "atomic_json", interrupt)
    with storage.lock():
        with pytest.raises(OSError):
            storage.commit([obj], {raw["id"]: raw["published_at"]}, {"run_id": "recover"})
    assert not (storage.directory / "state.json").exists()
    assert not (storage.directory / "news.json").exists()
    assert (storage.directory / ".pending.json").exists()
    monkeypatch.setattr(module, "atomic_json", original)
    with storage.lock():
        assert (storage.directory / "batches/recover.json").exists()
        assert not (storage.directory / ".pending.json").exists()


def test_preupgrade_latest_is_archived_before_replacement(tmp_path):
    storage = Storage(tmp_path / "news")
    write_batch(storage.directory, [article()])
    atomic_json(storage.directory / "run_report.json", {"run_id": "old"})
    with storage.lock():
        storage.commit([], {}, {"run_id": "new"})
    assert json.loads((storage.directory / "batches/old.json").read_text(encoding="utf-8"))["articles"] == [article()]


def test_48_hour_window_skips_stale_and_future_but_preserves_raw(tmp_path):
    old = {**article(2), "published_at": (NOW - timedelta(hours=49)).isoformat()}
    future = {**article(3), "published_at": (NOW + timedelta(seconds=1)).isoformat()}
    boundary = {**article(4), "published_at": (NOW - timedelta(hours=48)).isoformat()}
    pipeline, provider, store, _ = setup(tmp_path, [CLASSIFICATION, ANALYSIS], rows=[old, future, boundary])
    result = pipeline.run(COMPANY, "cfo")
    assert len(store.articles()) == 3 and result["counts"]["analyzed"] == 1
    assert store.feed("U_1", "cfo")[0]["id"] == boundary["id"]
    store.now = lambda: NOW + timedelta(seconds=1)
    assert store.feed("U_1", "cfo") == []


def test_feed_can_exclude_analyses_from_a_different_company_snapshot(tmp_path):
    pipeline, _, store, _ = setup(tmp_path, [CLASSIFICATION, ANALYSIS])
    pipeline.run(COMPANY, "cfo")
    assert len(store.feed("U_1", "cfo", company_hash=digest(COMPANY))) == 1
    assert store.feed("U_1", "cfo", company_hash=digest({**COMPANY, "version": "changed"})) == []


def test_pipeline_respects_active_parser_lock_without_model_calls(tmp_path):
    pipeline, provider, _, directory = setup(tmp_path, [])
    with Storage(directory).lock():
        result = pipeline.run(COMPANY, "cfo")
    assert result["errors"][0]["code"] == "news_unavailable"
    assert provider.requests == []

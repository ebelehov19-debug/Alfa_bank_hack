"""The real HTTP API, parser archive, pipeline and SQLite, with only the paid HTTP mocked."""

from datetime import datetime, timedelta, timezone
import json
import threading
import time
from uuid import uuid4

import httpx

import news_parser.__main__ as parser
from news_ai.config import Settings
from news_ai.pipeline import Pipeline
from news_ai.store import Store
from news_api import Application
from news_parser.models import Article, news_id
from news_parser.sources import SourceResult
from news_parser.storage import Storage


def test_archived_news_through_http_gemma_glm_persistence_and_question(tmp_path):
    now = datetime.now(timezone.utc)
    company = {"company_id": "A", "features": {"MAINOKVED_NAME": "Строительство"},
               "payments": {}, "limitations": ["Валюта и период платежей неизвестны."]}
    accepted_url, rejected_url = "https://www.moex.com/n111", "https://www.moex.com/n112"
    accepted_id = news_id("moex", accepted_url)
    news_dir = tmp_path / "news"
    storage = Storage(news_dir)
    articles = [Article(news_id("moex", url), "moex", url, title,
                        "Текст новости для проверки всего конвейера без настоящих обращений к моделям.",
                        now - timedelta(minutes=minutes), now)
                for url, title, minutes in [(accepted_url, "Изменение кредитных условий", 1),
                                             (rejected_url, "Нет деловой тематики", 2)]]
    with storage.lock():
        storage.commit(articles, {a.id: a.published_at.isoformat() for a in articles}, {"run_id": str(uuid4())})
        # A subsequent empty parser run must not erase pending AI inputs.
        storage.commit([], {}, {"run_id": str(uuid4())})

    calls = []
    settings = Settings(api_key="offline-test-key", retries=0)

    def model_response(request):
        assert str(request.url) == settings.base_url + "/chat/completions"
        payload = json.loads(request.content)
        data = json.loads(payload["messages"][1]["content"])["untrusted_data"]
        calls.append(payload["model"])
        if payload["model"] == settings.gemma_model:
            value = {"relevant": data["article"]["id"] == accepted_id, "confidence": 0.96,
                     "category": "Финансы", "reason": "Проверка тематического отбора."}
        elif "question" in data:
            assert data["company"] == company
            assert data["article"]["id"] == accepted_id
            value = {"answer": "Уточните условия действующих кредитных договоров."}
        else:
            assert payload["model"] == settings.glm_model
            assert data["role"] == "cfo" and data["company"] == company
            assert company["limitations"][0] in data["limitations"]
            value = {"type": "risk", "relevance": 84, "summary": "Изменились условия кредитования.",
                     "connection": "Для отрасли возможен рост стоимости новых заимствований.",
                     "impact": "Влияние зависит от условий договоров компании.",
                     "action": "Проверить сроки рефинансирования.",
                     "facts": ["Опубликовано изменение кредитных условий."],
                     "uncertainties": ["Размер долга компании неизвестен."]}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(value, ensure_ascii=False)}}]})

    provider = httpx.Client(transport=httpx.MockTransport(model_response))
    store_path = tmp_path / "ai.sqlite"
    pipeline = Pipeline(settings, Store(store_path), news_dir, client=provider)
    app = Application(settings=settings, companies={"A": company}, pipeline=pipeline,
                      news_directory=news_dir, frontend_directory=tmp_path,
                      news_collector=lambda directory: {"count": 0, "sources": {
                          source: {"status": "ok", "exported": 0} for source in ("moex", "tass")
                      }})
    server = app.create_server(port=0)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}", trust_env=False) as http:
            response = http.post("/api/analyze", json={"company_id": "A", "role": "cfo"})
            assert response.status_code == 202
            job_id = response.json()["job_id"]
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                job = http.get("/api/jobs/" + job_id).json()
                if job["status"] in {"completed", "failed"}:
                    break
                time.sleep(0.01)
            assert job["status"] == "completed", job
            assert job["result"]["counts"]["analyzed"] == 1
            assert job["result"]["counts"]["rejected"] == 1
            assert calls.count(settings.gemma_model) == 2
            assert calls.count(settings.glm_model) == 1
            item, = http.get("/api/feed", params={"company_id": "A", "role": "cfo"}).json()["items"]
            assert item["id"] == accepted_id and item["url"] == accepted_url
            assert item["bank_view"] is None and item["relevance"] == 84
            assert http.get("/api/feed", params={"company_id": "A", "role": "ceo"}).json()["items"] == []
            answer = http.post("/api/questions", json={"company_id": "A", "role": "cfo",
                               "news_id": accepted_id, "question": "Что проверить?"}).json()
            assert answer["answer"]
            assert answer["sources"][0]["url"] == accepted_url
            assert calls.count(settings.glm_model) == 2
            assert "offline-test-key" not in json.dumps({"job": job, "item": item, "answer": answer})
        # A fresh pipeline/connection must read durable results and skip all paid stages.
        restarted = Pipeline(settings, Store(store_path), news_dir, client=provider)
        result = restarted.run(company, "cfo")
        assert result["calls"] == 0
        assert Store(store_path).feed("A", "cfo")[0]["id"] == accepted_id
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        app.close()
        provider.close()


def test_analysis_collects_to_files_then_passes_exact_articles_to_both_models(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    news_dir = tmp_path / "new-news"
    company = {"company_id": "A", "features": {"MAINOKVED_NAME": "Строительство"},
               "limitations": ["Условия кредитования неизвестны."]}
    articles = [Article(news_id(source, url), source, url, title, text,
                        now - timedelta(minutes=index + 1), now)
                for index, (source, url, title, text) in enumerate([
                    ("moex", "https://www.moex.com/n123", "Изменение биржевых условий",
                     "Биржа опубликовала новые условия торгов для участников рынка.\nПодробности находятся в исходной публикации."),
                    ("tass", "https://tass.ru/ekonomika/456", "Новость о строительстве",
                     "Опубликована новость о новых условиях работы строительных компаний.\nИзменения требуют дополнительной проверки."),
                ])]
    expected = {article.id: article.to_dict() for article in articles}
    collected = []

    async def collect(settings, selected, state, observed, transport):
        assert selected == ["moex", "tass"] and state == {}
        assert not (news_dir / "news.json").exists()
        collected.append(True)
        return [SourceResult(source, articles=[a for a in articles if a.source == source],
                             discovery_ok=True) for source in selected]

    # Exercise Application's default collector, its real runner and Storage.
    # Only retrieval and paid model responses are replaced.
    monkeypatch.setattr(parser, "collect", collect)
    settings = Settings(api_key="offline-test-key", retries=0)
    model_calls = []

    def model_response(request):
        assert collected == [True]
        files = json.loads((news_dir / "news.json").read_text(encoding="utf-8"))
        assert {row["id"] for row in files} == set(expected)
        payload = json.loads(request.content)
        data = json.loads(payload["messages"][1]["content"])["untrusted_data"]
        article = data["article"]
        original = expected[article["id"]]
        for field in ("id", "title", "text", "url", "source", "published_at"):
            assert article[field] == original[field], field
        model_calls.append((payload["model"], article["id"]))
        if payload["model"] == settings.gemma_model:
            value = {"relevant": True, "confidence": 0.9, "category": "Экономика", "reason": "Деловая новость."}
        else:
            assert payload["model"] == settings.glm_model
            assert data["company"] == company and data["role"] == "cfo"
            value = {"type": "neutral", "relevance": 30, "summary": "Новые условия требуют проверки.",
                     "connection": "Компания работает в строительной отрасли.",
                     "impact": "Прямое влияние пока не подтверждено.", "action": "Проверить применимость условий.",
                     "facts": [original["title"]], "uncertainties": ["Условия договоров неизвестны."]}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(value, ensure_ascii=False)}}]})

    provider = httpx.Client(transport=httpx.MockTransport(model_response))
    store = Store(tmp_path / "ai.sqlite")
    app = Application(settings=settings, companies={"A": company},
                      pipeline=Pipeline(settings, store, news_dir, client=provider),
                      news_directory=news_dir, frontend_directory=tmp_path)
    server = app.create_server(port=0)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}", trust_env=False) as http:
            assert http.get("/api/health").json()["news_available"] is False
            response = http.post("/api/analyze", json={"company_id": "A", "role": "cfo"})
            assert response.status_code == 202
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                job = http.get("/api/jobs/" + response.json()["job_id"]).json()
                if job["status"] in {"completed", "failed"}:
                    break
                time.sleep(0.01)
            assert job["status"] == "completed", job
            assert job["result"]["collection"]["count"] == 2
            assert job["result"]["counts"]["imported"] == 2
            assert job["result"]["counts"]["analyzed"] == 2
            assert job["result"]["errors"] == []
            assert http.get("/api/health").json()["news_available"] is True
            assert {item["id"] for item in http.get("/api/feed?company_id=A&role=cfo").json()["items"]} == set(expected)
        for source in ("moex", "tass"):
            saved, = json.loads((news_dir / f"{source}.json").read_text(encoding="utf-8"))
            assert saved == next(article.to_dict() for article in articles if article.source == source)
            assert (settings.gemma_model, saved["id"]) in model_calls
            assert (settings.glm_model, saved["id"]) in model_calls
        assert len(list((news_dir / "batches").glob("*.json"))) == 1
        assert len(model_calls) == 4
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        app.close()
        provider.close()

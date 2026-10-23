"""Offline HTTP checks for the model API boundary and its background jobs."""

import json
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from news_api import Application
from news_ai.store import digest


_UNSET = object()


class FakeSettings:
    def __init__(self, configured=True):
        self.configured = configured

    def missing(self):
        return [] if self.configured else ["LLM_API_KEY"]

    def public(self):
        return {
            "configured": self.configured,
            "missing": self.missing(),
            "models": {"classifier": "test-gemma", "analyst": "test-glm"},
        }


class FakeStore:
    def __init__(self):
        self.items = {}
        self.calls = []

    def feed(self, company_id, role, company_hash=None):
        self.calls.append((company_id, role, company_hash))
        return self.items.get((company_id, role, company_hash), [])


class FakePipeline:
    def __init__(self):
        self.store = FakeStore()
        self.calls = []
        self.run_error = None
        self.question_error = None

    def run(self, company, role, limit=None):
        self.calls.append(("run", company, role, limit))
        if self.run_error:
            raise self.run_error
        item = {"id": "news-1", "headline": "Изменение ставки", "role": role}
        self.store.items[(company["company_id"], role, digest(company))] = [item]
        return {"classified": 2, "relevant": 1, "analyzed": 1}

    def question(self, company, role, news_id, question):
        self.calls.append(("question", company, role, news_id, question))
        if self.question_error:
            raise self.question_error
        return {"answer": "Проверьте условия кредитования.", "sources": ["https://example.test/news/1"]}


class RunningAPI:
    def __init__(self, app, server, pipeline, collection_calls):
        self.app = app
        self.server = server
        self.pipeline = pipeline
        self.collection_calls = collection_calls
        self.origin = f"http://127.0.0.1:{server.server_address[1]}"

    def request(self, path, method="GET", body=_UNSET, raw=None, headers=None):
        request_headers = dict(headers or {})
        data = raw
        if body is not _UNSET:
            data = json.dumps(body).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        request = Request(self.origin + path, data=data, method=method, headers=request_headers)
        try:
            response = urlopen(request, timeout=3)
        except HTTPError as error:
            response = error
        with response:
            content = response.read()
            if "application/json" in response.headers.get("Content-Type", ""):
                content = json.loads(content)
            return response.status, content

    def wait_for_job(self, job_id):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            status, job = self.request(f"/api/jobs/{job_id}")
            assert status == 200
            if job["status"] in {"completed", "failed"}:
                return job
            assert job["status"] in {"queued", "running"}
            time.sleep(0.01)
        pytest.fail("API background job did not finish within 3 seconds")


@pytest.fixture
def api_factory(tmp_path):
    instances = []
    companies = {
        "A": {"company_id": "A", "features": {"MAINOKVED_NAME": "Строительство"}, "payments": {}},
        "B": {"company_id": "B", "features": {"MAINOKVED_NAME": "Торговля"}, "payments": {}},
    }

    def create(configured=True, news_collector=None, news_available=True):
        directory = tmp_path / str(len(instances))
        directory.mkdir()
        frontend = directory / "frontend"
        frontend.mkdir()
        (frontend / "index.html").write_text("<!doctype html><title>Test frontend</title>", encoding="utf-8")
        (frontend / "app.js").write_text("console.log('test');", encoding="utf-8")
        (frontend / ".env").write_text("LLM_API_KEY=private-static-key", encoding="utf-8")
        (frontend / "server.py").write_text("private_backend_source = True", encoding="utf-8")
        news = directory / "news"
        news.mkdir()
        if news_available:
            (news / "news.json").write_text("[]", encoding="utf-8")
        pipeline = FakePipeline()
        collection_calls = []

        def collect(directory):
            collection_calls.append(directory)
            if news_collector is not None:
                return news_collector(directory)
            return {"count": 0, "sources": {
                source: {"status": "ok", "exported": 0} for source in ("moex", "tass")
            }}

        app = Application(
            settings=FakeSettings(configured),
            context_db=directory / "context.sqlite",
            ai_db=directory / "news_ai.sqlite",
            news_directory=news,
            frontend_directory=frontend,
            companies=companies,
            pipeline=pipeline,
            news_collector=collect,
        )
        server = app.create_server(host="127.0.0.1", port=0)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        instances.append((app, server, thread))
        return RunningAPI(app, server, pipeline, collection_calls)

    yield create

    for app, server, thread in reversed(instances):
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        app.close()


def test_read_api_and_frontend_work_without_model_key(api_factory):
    api = api_factory(configured=False)
    status, health = api.request("/api/health")
    assert status == 200
    assert health["configured"] is False
    assert health["missing"] == ["LLM_API_KEY"]
    assert health["models"] == {"classifier": "test-gemma", "analyst": "test-glm"}
    assert "news_available" in health

    status, body = api.request("/api/companies")
    assert status == 200
    assert {company["company_id"] for company in body["companies"]} == {"A", "B"}
    assert all("features" in company for company in body["companies"])
    assert api.request("/api/feed?company_id=A&role=cfo") == (200, {"items": []})
    status, frontend = api.request("/")
    assert status == 200
    assert b"Test frontend" in frontend
    assert api.pipeline.calls == []
    assert api.collection_calls == []


@pytest.mark.parametrize("endpoint,body", [
    ("/api/analyze", {"company_id": "A", "role": "cfo"}),
    ("/api/questions", {"company_id": "A", "role": "cfo", "news_id": "news-1", "question": "Как влияет ставка?"}),
])
def test_missing_key_rejects_model_calls_without_executing_pipeline(api_factory, endpoint, body):
    api = api_factory(configured=False)
    status, response = api.request(endpoint, "POST", body)
    assert status == 503
    assert response.get("error")
    assert api.pipeline.calls == []
    assert api.collection_calls == []


def test_analysis_job_and_feed_keep_company_and_role_context(api_factory):
    api = api_factory()
    status, body = api.request("/api/analyze", "POST", {"company_id": "A", "role": "cfo", "limit": 2})
    assert status == 202
    job = api.wait_for_job(body["job_id"])
    assert job["id"] == body["job_id"]
    assert job["status"] == "completed"
    assert job["stage"] == "completed"
    assert job["result"] == {"classified": 2, "relevant": 1, "analyzed": 1,
                             "collection": {"count": 0, "sources": {
                                 source: {"status": "ok", "exported": 0} for source in ("moex", "tass")
                             }}}
    assert api.collection_calls == [api.app.news_directory]
    assert not job.get("error")
    assert api.pipeline.calls[0] == (
        "run", {"company_id": "A", "features": {"MAINOKVED_NAME": "Строительство"}, "payments": {}}, "cfo", 2,
    )
    status, feed = api.request("/api/feed?company_id=A&role=cfo")
    assert status == 200
    assert [item["id"] for item in feed["items"]] == ["news-1"]
    company = api.pipeline.calls[0][1]
    assert api.pipeline.store.calls[-1] == ("A", "cfo", digest(company))
    assert api.request("/api/feed?company_id=B&role=cfo") == (200, {"items": []})
    assert api.request("/api/feed?company_id=A&role=ceo") == (200, {"items": []})


def test_failed_job_does_not_expose_provider_exception_or_key(api_factory):
    api = api_factory()
    secret = "secret-provider-key-123"
    api.pipeline.run_error = RuntimeError(f"https://provider.test/?api_key={secret}")
    status, body = api.request("/api/analyze", "POST", {"company_id": "A", "role": "treasury"})
    assert status == 202
    job = api.wait_for_job(body["job_id"])
    assert job["status"] == "failed"
    assert job.get("error")
    public_job = json.dumps(job)
    assert secret not in public_job
    assert "provider.test" not in public_job
    assert "Traceback" not in public_job


def test_polling_remains_available_while_model_call_runs(api_factory):
    api = api_factory()
    started = threading.Event()
    release = threading.Event()
    original_run = api.pipeline.run

    def blocking_run(company, role, limit=None):
        started.set()
        assert release.wait(timeout=5), "test did not release model call"
        return original_run(company, role, limit)

    api.pipeline.run = blocking_run
    try:
        status, body = api.request("/api/analyze", "POST", {"company_id": "A", "role": "cfo"})
        assert status == 202
        assert started.wait(timeout=1)
        status, job = api.request(f"/api/jobs/{body['job_id']}")
        assert status == 200
        assert job["status"] == "running"
        assert job["stage"] == "analyzing"
        assert api.request("/api/health")[0] == 200
        status, error = api.request("/api/analyze", "POST", {"company_id": "B", "role": "ceo"})
        assert status == 409
        assert error.get("error")
    finally:
        release.set()
    assert api.wait_for_job(body["job_id"])["status"] == "completed"


def test_collection_runs_before_analysis_even_without_saved_news(api_factory):
    started = threading.Event()
    release = threading.Event()

    def collect(directory):
        assert not (directory / "news.json").exists()
        started.set()
        assert release.wait(timeout=5), "test did not release collection"
        return {"count": 0, "sources": {
            source: {"status": "ok", "exported": 0} for source in ("moex", "tass")
        }}

    api = api_factory(news_collector=collect, news_available=False)
    assert api.request("/api/health")[1]["news_available"] is False
    try:
        status, body = api.request("/api/analyze", "POST", {"company_id": "A", "role": "cfo"})
        assert status == 202
        assert started.wait(timeout=1)
        status, job = api.request(f"/api/jobs/{body['job_id']}")
        assert status == 200 and job["status"] == "running" and job["stage"] == "parsing"
        assert api.pipeline.calls == []
        assert api.request("/api/health")[0] == 200
        assert api.request("/api/analyze", "POST", {"company_id": "B", "role": "ceo"})[0] == 409
        assert api.request("/api/questions", "POST", {
            "company_id": "A", "role": "cfo", "news_id": "news-1", "question": "Что произошло?",
        })[0] == 409
    finally:
        release.set()
    assert api.wait_for_job(body["job_id"])["status"] == "completed"
    assert api.pipeline.calls[0][0] == "run"


def test_failed_collection_skips_models_hides_details_and_releases_lock(api_factory):
    def collect(directory):
        raise RuntimeError("private-cookie-and-upstream-token")

    api = api_factory(news_collector=collect)
    status, body = api.request("/api/analyze", "POST", {"company_id": "A", "role": "cfo"})
    assert status == 202
    job = api.wait_for_job(body["job_id"])
    assert job["status"] == "failed"
    assert job["error"] and "private-cookie" not in json.dumps(job)
    assert api.pipeline.calls == []
    api.app.news_collector = lambda directory: {"count": 0, "sources": {
        source: {"status": "ok", "exported": 0} for source in ("moex", "tass")
    }}
    status, body = api.request("/api/analyze", "POST", {"company_id": "A", "role": "cfo"})
    assert status == 202
    assert api.wait_for_job(body["job_id"])["status"] == "completed"


@pytest.mark.parametrize("statuses,expected", [(("error", "error"), "failed"),
                                                (("ok", "error"), "completed"),
                                                (("partial", "ok"), "completed")])
def test_source_failures_stop_all_failed_collection_or_report_partial_result(api_factory, statuses, expected):
    def collect(directory):
        return {"count": 1, "sources": {
            source: {"status": status, "exported": 1 if status != "error" else 0,
                     "errors": ["private-parser-diagnostic"]}
            for source, status in zip(("moex", "tass"), statuses)
        }}

    api = api_factory(news_collector=collect)
    status, body = api.request("/api/analyze", "POST", {"company_id": "A", "role": "cfo"})
    assert status == 202
    job = api.wait_for_job(body["job_id"])
    assert job["status"] == expected
    assert "private-parser-diagnostic" not in json.dumps(job)
    if expected == "failed":
        assert api.pipeline.calls == []
    else:
        assert len(api.pipeline.calls) == 1
        assert job["result"]["collection"]["count"] == 1
        error, = job["result"]["errors"]
        assert error["stage"] == "parsing" and error["code"] == "source_incomplete"


def test_question_passes_profile_role_and_news_context_to_pipeline(api_factory):
    api = api_factory()
    status, answer = api.request("/api/questions", "POST", {
        "company_id": "B", "role": "ceo", "news_id": "news-1", "question": "Какие действия нужны?",
    }, headers={"Origin": api.origin})
    assert status == 200
    assert answer == {"answer": "Проверьте условия кредитования.", "sources": ["https://example.test/news/1"]}
    call = api.pipeline.calls[0]
    assert call[0] == "question"
    assert call[1]["company_id"] == "B"
    assert call[2:] == ("ceo", "news-1", "Какие действия нужны?")


def test_question_failure_has_safe_public_error(api_factory):
    api = api_factory()
    api.pipeline.question_error = RuntimeError("Authorization: Bearer private-upstream-key")
    status, body = api.request("/api/questions", "POST", {
        "company_id": "A", "role": "cfo", "news_id": "news-1", "question": "Что изменилось?",
    })
    assert status >= 500
    assert body.get("error")
    assert "private-upstream-key" not in json.dumps(body)


def test_question_for_unanalyzed_news_returns_safe_not_found(api_factory):
    api = api_factory()
    api.pipeline.question_error = LookupError("private-profile-or-news-details")
    status, body = api.request("/api/questions", "POST", {
        "company_id": "A", "role": "cfo", "news_id": "not-analyzed", "question": "Что изменилось?",
    })
    assert status == 404
    assert body.get("error")
    assert "private-profile-or-news-details" not in json.dumps(body)


@pytest.mark.parametrize("path,method,body,expected", [
    ("/api/feed?company_id=unknown&role=cfo", "GET", _UNSET, 404),
    ("/api/feed?company_id=A&role=admin", "GET", _UNSET, 400),
    ("/api/analyze", "POST", {"company_id": "unknown", "role": "cfo"}, 404),
    ("/api/analyze", "POST", {"company_id": "A", "role": "admin"}, 400),
    ("/api/analyze", "POST", {"company_id": "A", "role": "cfo", "limit": -1}, 400),
    ("/api/analyze", "POST", {"company_id": "A", "role": "cfo", "limit": True}, 400),
    ("/api/analyze", "POST", [], 400),
    ("/api/questions", "POST", {"company_id": "A", "role": "cfo", "news_id": "news-1", "question": "  "}, 400),
    ("/api/jobs/does-not-exist", "GET", _UNSET, 404),
    ("/api/no-such-endpoint", "GET", _UNSET, 404),
])
def test_invalid_requests_fail_before_pipeline(api_factory, path, method, body, expected):
    api = api_factory()
    status, response = api.request(path, method, body)
    assert status == expected
    assert response.get("error")
    assert api.pipeline.calls == []


def test_malformed_json_is_bad_request(api_factory):
    api = api_factory()
    status, response = api.request("/api/analyze", "POST", raw=b'{"company_id":', headers={"Content-Type": "application/json"})
    assert status == 400
    assert response.get("error")
    assert api.pipeline.calls == []


def test_mutations_reject_foreign_origin_and_require_json(api_factory):
    api = api_factory()
    payload = {"company_id": "A", "role": "cfo"}
    status, response = api.request("/api/analyze", "POST", payload, headers={"Origin": "https://attacker.test"})
    assert status == 403
    assert response.get("error")
    status, response = api.request("/api/analyze", "POST", raw=json.dumps(payload).encode(), headers={"Content-Type": "text/plain"})
    assert status in {400, 415}
    assert response.get("error")
    assert api.pipeline.calls == []


@pytest.mark.parametrize("path", ["/.env", "/server.py", "/data/context.sqlite", "/%2e%2e/.env", "/backend/news_api.py"])
def test_static_server_does_not_expose_secrets_or_backend_files(api_factory, path):
    api = api_factory()
    status, body = api.request(path)
    assert status in {400, 403, 404}
    public_body = body.decode("utf-8") if isinstance(body, bytes) else json.dumps(body)
    assert "private-static-key" not in public_body
    assert "private_backend_source" not in public_body

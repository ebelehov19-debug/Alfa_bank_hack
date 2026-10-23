"""Local, same-origin frontend and API for the Gemma -> GLM news pipeline."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import mimetypes
import os
from pathlib import Path
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit
from uuid import uuid4

from core import profile
from news_ai.config import Settings
from news_ai.pipeline import Pipeline
from news_ai.store import Store, digest
from news_parser.runner import collect_news


ROOT = Path(__file__).resolve().parents[1]
ROLES = {"cfo", "ceo", "treasury"}
STATIC_FILES = {"index.html", "app.js", "style.css", "data.js"}


class APIError(Exception):
    def __init__(self, status, message):
        self.status = status
        self.message = message


class Application:
    def __init__(self, settings=None, context_db=None, ai_db=None, news_directory=None,
                 frontend_directory=None, companies=None, pipeline=None, news_collector=None):
        self.settings = settings if settings is not None else Settings.from_env()
        self.context_db = Path(context_db or os.getenv("CONTEXT_DB", ROOT / "data/context.sqlite"))
        self.news_directory = Path(news_directory or os.getenv("NEWS_DIRECTORY", ROOT / "data/news"))
        self.frontend_directory = Path(frontend_directory or ROOT / "frontend/dist")
        self._companies = deepcopy(companies) if companies is not None else self._load_companies()
        self.pipeline = pipeline if pipeline is not None else Pipeline(
            self.settings, Store(Path(ai_db or os.getenv("AI_DB", ROOT / "data/ai.sqlite"))),
            self.news_directory,
        )
        self.news_collector = news_collector if news_collector is not None else collect_news
        self._jobs = {}
        self._jobs_lock = threading.Lock()
        self._model_lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="news-analysis")

    def _load_companies(self):
        """Keep the existing three real profiles; use the imported DB when present."""
        path = self.frontend_directory / "data.js"
        profiles = []
        if path.exists():
            content = path.read_text(encoding="utf-8")
            prefix = "window.CONTEXT_DATA = "
            if not content.startswith(prefix):
                raise ValueError("Unrecognized company snapshot")
            profiles = json.loads(content[len(prefix):].strip().removesuffix(";"))
        snapshots = {item["company_id"]: item for item in profiles}
        selected = os.getenv("CONTEXT_COMPANY_IDS", "").strip()
        company_ids = [item.strip() for item in selected.split(",") if item.strip()] if selected else list(snapshots)
        if not self.context_db.exists():
            if selected and any(uid not in snapshots for uid in company_ids):
                raise ValueError("Import CSV into the context database before selecting additional companies")
            return {uid: snapshots[uid] for uid in company_ids}
        # mode=ro prevents accidentally creating a new empty context database.
        import sqlite3
        with sqlite3.connect(self.context_db.resolve().as_uri() + "?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            return {uid: profile(db, uid) for uid in company_ids}

    def context(self, company_id, role):
        if not isinstance(company_id, str) or not isinstance(role, str) or role not in ROLES:
            raise APIError(400, "Укажите компанию и допустимую роль пользователя.")
        if company_id not in self._companies:
            raise APIError(404, "Компания не найдена.")
        return deepcopy(self._companies[company_id])

    def require_models(self):
        if self.settings.missing():
            raise APIError(503, "Анализ станет доступен после настройки подключения моделей.")

    def health(self):
        public = self.settings.public()
        return {
            "configured": not self.settings.missing(),
            "missing": self.settings.missing(),
            "models": public.get("models", {}),
            "news_available": (self.news_directory / "news.json").exists()
                              or any((self.news_directory / "batches").glob("*.json")),
        }

    def submit(self, company, role, limit):
        self.require_models()
        if not self._model_lock.acquire(blocking=False):
            raise APIError(409, "Дождитесь завершения текущего анализа или ответа.")
        job_id = str(uuid4())
        with self._jobs_lock:
            # Jobs are progress notices. The durable results live in SQLite.
            while len(self._jobs) >= 100:
                finished = next((key for key, value in self._jobs.items()
                                 if value["status"] in {"completed", "failed"}), None)
                if finished is None:
                    break
                del self._jobs[finished]
            self._jobs[job_id] = {"id": job_id, "status": "queued", "stage": "queued",
                                  "result": None, "error": None}
        try:
            self._executor.submit(self._run_job, job_id, company, role, limit)
        except Exception:
            self._model_lock.release()
            raise
        return job_id

    def _run_job(self, job_id, company, role, limit):
        with self._jobs_lock:
            self._jobs[job_id].update(status="running", stage="parsing")
        stage = "parsing"
        try:
            report = self.news_collector(self.news_directory)
            # Keep detailed diagnostics in run_report.json. Only known, safe
            # fields and messages cross the public API boundary.
            collection = {"count": report["count"], "sources": {
                source: {"status": report["sources"][source]["status"],
                         "exported": report["sources"][source]["exported"]}
                for source in ("moex", "tass")
            }}
            if all(source["status"] == "error" for source in collection["sources"].values()):
                raise RuntimeError("News sources unavailable")
            collection_errors = [
                {"stage": "parsing", "code": "source_incomplete", "source": source,
                 "message": f"Источник {source.upper()} обработан не полностью. Анализ выполнен по доступным публикациям."}
                for source, value in collection["sources"].items() if value["status"] != "ok"
            ]
            stage = "analyzing"
            with self._jobs_lock:
                self._jobs[job_id]["stage"] = stage
            result = self.pipeline.run(company, role, limit=limit)
            result["collection"] = collection
            if collection_errors:
                result.setdefault("errors", []).extend(collection_errors)
                if "counts" in result:
                    result["counts"]["errors"] = len(result["errors"])
            with self._jobs_lock:
                self._jobs[job_id].update(status="completed", stage="completed", result=result)
        except Exception:
            # Provider exceptions may include Authorization headers, body or URLs.
            error = ("Не удалось собрать свежие новости. Проверьте доступность источников и установку Chromium; сохранённые результаты доступны в ленте."
                     if stage == "parsing" else
                     "Не удалось завершить анализ. Сохранённые результаты доступны в ленте; повторите попытку позже.")
            with self._jobs_lock:
                self._jobs[job_id].update(status="failed", stage="failed", error=error)
        finally:
            self._model_lock.release()

    def job(self, job_id):
        with self._jobs_lock:
            if job_id not in self._jobs:
                raise APIError(404, "Задача не найдена.")
            return deepcopy(self._jobs[job_id])

    def question(self, company, role, news_id, question):
        self.require_models()
        if not self._model_lock.acquire(blocking=False):
            raise APIError(409, "Дождитесь завершения текущего анализа или ответа.")
        try:
            return self.pipeline.question(company, role, news_id, question)
        except (LookupError, KeyError):
            raise APIError(404, "Сначала выполните анализ этой новости для выбранной компании и роли.") from None
        except Exception:
            raise APIError(502, "Не удалось получить ответ. Попробуйте позже.") from None
        finally:
            self._model_lock.release()

    def close(self):
        self._executor.shutdown(wait=True, cancel_futures=True)

    def create_server(self, host="127.0.0.1", port=8080):
        app = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "AlfaContext"

            def setup(self):
                super().setup()
                self.connection.settimeout(15)

            def log_message(self, format, *args):
                # In particular, don't put question text or arbitrary URLs in logs.
                pass

            def respond(self, status, body, content_type="application/json; charset=utf-8"):
                payload = json.dumps(body, ensure_ascii=False).encode("utf-8") if isinstance(body, (dict, list)) else body
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")
                self.end_headers()
                self.wfile.write(payload)

            def handle_request(self, action):
                try:
                    host = urlsplit("http://" + self.headers.get("Host", "")).hostname
                    if host not in {"localhost", "127.0.0.1", "::1", str(self.server.server_address[0])}:
                        raise APIError(403, "Недопустимый адрес сервера.")
                    action()
                except APIError as error:
                    self.respond(error.status, {"error": error.message})
                except (BrokenPipeError, ConnectionResetError):
                    pass
                except Exception:
                    self.respond(500, {"error": "Не удалось обработать запрос."})

            def do_GET(self):
                self.handle_request(self.get)

            def get(self):
                request = urlsplit(self.path)
                path = unquote(request.path)
                query = parse_qs(request.query)
                if path == "/api/health":
                    self.respond(200, app.health())
                elif path == "/api/companies":
                    self.respond(200, {"companies": list(app._companies.values())})
                elif path == "/api/feed":
                    company_id = query.get("company_id", [None])[0]
                    role = query.get("role", [None])[0]
                    company = app.context(company_id, role)
                    self.respond(200, {"items": app.pipeline.store.feed(company_id, role, company_hash=digest(company))})
                elif path.startswith("/api/jobs/"):
                    self.respond(200, app.job(path.removeprefix("/api/jobs/")))
                elif path.startswith("/api/"):
                    raise APIError(404, "API-метод не найден.")
                else:
                    name = "index.html" if path == "/" else path.removeprefix("/")
                    if name not in STATIC_FILES:
                        raise APIError(404, "Страница не найдена.")
                    file = app.frontend_directory / name
                    if not file.is_file() or file.is_symlink():
                        raise APIError(404, "Страница не найдена.")
                    content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
                    self.respond(200, file.read_bytes(), content_type + "; charset=utf-8")

            def do_POST(self):
                self.handle_request(self.post)

            def post(self):
                path = urlsplit(self.path).path
                if path not in {"/api/analyze", "/api/questions"}:
                    raise APIError(404, "API-метод не найден.")
                origin = self.headers.get("Origin")
                if (origin is not None and origin != "http://" + self.headers.get("Host", "")) or self.headers.get("Sec-Fetch-Site") == "cross-site":
                    raise APIError(403, "Запрос должен исходить из приложения.")
                if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                    raise APIError(415, "Ожидается JSON.")
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    raise APIError(400, "Некорректная длина запроса.") from None
                if not 0 < length <= 16384:
                    raise APIError(400, "Пустой или слишком большой запрос.")
                try:
                    body = json.loads(self.rfile.read(length))
                except (ValueError, UnicodeDecodeError):
                    raise APIError(400, "Некорректный JSON.") from None
                if not isinstance(body, dict):
                    raise APIError(400, "Ожидается объект JSON.")
                company = app.context(body.get("company_id"), body.get("role"))
                role = body["role"]
                if path == "/api/analyze":
                    limit = body.get("limit")
                    if limit is not None and (type(limit) is not int or not 1 <= limit <= 50):
                        raise APIError(400, "Лимит новостей должен быть целым числом от 1 до 50.")
                    self.respond(202, {"job_id": app.submit(company, role, limit)})
                else:
                    news_id = body.get("news_id")
                    question = body.get("question")
                    if not isinstance(news_id, str) or not news_id.strip() or len(news_id) > 128:
                        raise APIError(400, "Укажите новость.")
                    if not isinstance(question, str) or not question.strip() or len(question) > 500:
                        raise APIError(400, "Вопрос должен содержать от 1 до 500 символов.")
                    self.respond(200, app.question(company, role, news_id, question.strip()))

        return ThreadingHTTPServer((host, port), Handler)


def main():
    parser = argparse.ArgumentParser(description="Alfa Context: frontend + Gemma/GLM API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8080, type=int)
    parser.add_argument("--context-db", type=Path)
    parser.add_argument("--ai-db", type=Path)
    parser.add_argument("--news-dir", type=Path)
    args = parser.parse_args()
    app = Application(context_db=args.context_db, ai_db=args.ai_db, news_directory=args.news_dir)
    server = app.create_server(args.host, args.port)
    print(f"Alfa Context: http://{args.host}:{server.server_port}", flush=True)
    if app.settings.missing():
        print("Настройте подключение моделей в alfa-context/.env и перезапустите сервер.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        app.close()


if __name__ == "__main__":
    main()

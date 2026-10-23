"""Durable raw inputs and per-stage caches, independent of parser deduplication."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3


def compact(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value) -> str:
    return sha256(compact(value).encode("utf-8")).hexdigest()


def content_hash(article: dict) -> str:
    return digest({key: value for key, value in article.items() if key != "collected_at"})


class Store:
    def __init__(self, db_path, now=None):
        self.path = Path(db_path)
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS raw_articles(
                    id TEXT PRIMARY KEY, content_hash TEXT NOT NULL, body TEXT NOT NULL,
                    published_at TEXT NOT NULL, collected_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS raw_versions(
                    id TEXT NOT NULL, content_hash TEXT NOT NULL, body TEXT NOT NULL,
                    PRIMARY KEY(id,content_hash));
                CREATE TABLE IF NOT EXISTS classifications(
                    cache_key TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS analyses(
                    cache_key TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS feed_entries(
                    company_id TEXT NOT NULL, role TEXT NOT NULL, article_id TEXT NOT NULL,
                    company_hash TEXT NOT NULL, cache_key TEXT NOT NULL,
                    PRIMARY KEY(company_id,role,article_id));
                CREATE TABLE IF NOT EXISTS answers(
                    cache_key TEXT PRIMARY KEY, body TEXT NOT NULL);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def import_articles(self, articles: list[dict]) -> int:
        imported = 0
        with self.connect() as db:
            for article in articles:
                fingerprint = content_hash(article)
                body = compact(article)
                cursor = db.execute("INSERT OR IGNORE INTO raw_versions VALUES(?,?,?)", (article["id"], fingerprint, body))
                imported += cursor.rowcount
                db.execute("""INSERT INTO raw_articles VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                    content_hash=excluded.content_hash, body=excluded.body,
                    published_at=excluded.published_at, collected_at=excluded.collected_at
                    WHERE excluded.collected_at >= raw_articles.collected_at""",
                           (article["id"], fingerprint, body, article["published_at"], article["collected_at"]))
        return imported

    def articles(self) -> list[dict]:
        with self.connect() as db:
            return [json.loads(row["body"]) for row in db.execute(
                "SELECT body FROM raw_articles ORDER BY published_at DESC,id")]

    def get_cache(self, stage: str, key: str):
        table = self._table(stage)
        with self.connect() as db:
            row = db.execute(f"SELECT body FROM {table} WHERE cache_key=?", (key,)).fetchone()
            return json.loads(row["body"]) if row else None

    def put_cache(self, stage: str, key: str, value: dict):
        table = self._table(stage)
        with self.connect() as db:
            db.execute(f"INSERT OR REPLACE INTO {table} VALUES(?,?)", (key, compact(value)))

    @staticmethod
    def _table(stage: str):
        if stage not in {"classifications", "analyses", "answers"}:
            raise ValueError("Invalid cache stage")
        return stage

    def activate(self, company_id: str, role: str, article_id: str, company_hash: str, key: str):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO feed_entries VALUES(?,?,?,?,?)",
                       (company_id, role, article_id, company_hash, key))

    def reject(self, company_id: str, role: str, article_id: str):
        with self.connect() as db:
            db.execute("DELETE FROM feed_entries WHERE company_id=? AND role=? AND article_id=?",
                       (company_id, role, article_id))

    def feed(self, company_id: str, role: str, company_hash: str | None = None) -> list[dict]:
        with self.connect() as db:
            sql = """SELECT a.body FROM feed_entries f JOIN analyses a ON a.cache_key=f.cache_key
                WHERE f.company_id=? AND f.role=?"""
            params = [company_id, role]
            if company_hash is not None:
                sql += " AND f.company_hash=?"
                params.append(company_hash)
            rows = db.execute(sql, params).fetchall()
        items = [json.loads(row["body"]) for row in rows]
        now = self.now()
        cutoff = now - timedelta(hours=48)
        items = [item for item in items if cutoff <= datetime.fromisoformat(item["published_at"]) <= now]
        items.sort(key=lambda item: (item["relevance"], item["published_at"], item["id"]), reverse=True)
        return [{key: value for key, value in item.items() if not key.startswith("_")} for item in items]

    def context(self, company_id: str, role: str, article_id: str):
        with self.connect() as db:
            row = db.execute("""SELECT a.body,f.cache_key,f.company_hash FROM feed_entries f
                JOIN analyses a ON a.cache_key=f.cache_key
                WHERE f.company_id=? AND f.role=? AND f.article_id=?""", (company_id, role, article_id)).fetchone()
            if not row:
                return None
            item = json.loads(row["body"])
            # The analysis stores the exact raw-content version internally, never model-authored provenance.
            version = db.execute("SELECT body FROM raw_versions WHERE id=? AND content_hash=?",
                                 (article_id, item["_content_hash"])).fetchone()
            if not version:
                return None
            return {"analysis": item, "article": json.loads(version["body"]),
                    "cache_key": row["cache_key"], "company_hash": row["company_hash"]}

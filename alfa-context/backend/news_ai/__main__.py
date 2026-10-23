import argparse
import json
from pathlib import Path
import sqlite3

from .config import PROJECT_ROOT, Settings
from .pipeline import Pipeline
from .store import Store


def main():
    parser = argparse.ArgumentParser(description="Gemma topic filter -> GLM company analysis of saved parser news")
    parser.add_argument("--company", required=True)
    parser.add_argument("--role", default="cfo")
    parser.add_argument("--db", type=Path, default=PROJECT_ROOT / "data/context.sqlite")
    parser.add_argument("--news-dir", type=Path, default=PROJECT_ROOT / "data/news")
    parser.add_argument("--store", type=Path, default=PROJECT_ROOT / "data/ai.sqlite")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    try:
        from core import connect, profile
        if not args.db.is_file():
            parser.exit(1, "Company database not found; import the CSV data first.\n")
        with connect(args.db) as db:
            company = profile(db, args.company)
        result = Pipeline(Settings.from_env(), Store(args.store), args.news_dir).run(company, args.role, args.limit)
    except (ValueError, OSError, RuntimeError, sqlite3.Error):
        parser.exit(1, "Pipeline could not start; check configuration, company and local data files.\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(2 if result["errors"] else 0)


if __name__ == "__main__":
    main()

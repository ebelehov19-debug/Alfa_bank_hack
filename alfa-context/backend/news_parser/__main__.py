from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta
import json
import logging
import os
import sys
from uuid import uuid4

from .models import Settings, SOURCES, UTC
from .sources import ADAPTERS
from .storage import Storage
from .transport import Transport


async def collect(settings, selected, state, now, transport):
    cutoff = settings.cutoff(now)
    return await asyncio.gather(*(
        ADAPTERS[name](transport, settings).collect(
            cutoff, now, {key for key in state if key.startswith(name + ":")})
        for name in selected
    ))


async def run(args):
    settings = Settings(hours=args.hours, limit=args.limit, timeout=args.timeout,
                        delay=args.delay, retries=args.retries, max_pages=args.max_pages,
                        browser=not args.no_browser)
    storage = Storage(args.output)
    now = datetime.now(UTC)
    with storage.lock():
        state = storage.state(now - timedelta(hours=48))
        async with Transport(settings) as transport:
            results = await collect(settings, args.sources, state, now, transport)
        articles = [a for r in results for a in r.articles]
        for article in articles:
            state[article.id] = article.published_at.isoformat()
        report = {"schema_version": 1, "run_id": str(uuid4()),
                  "started_at": now.isoformat(), "finished_at": datetime.now(UTC).isoformat(),
                  "window_start": settings.cutoff(now).isoformat(), "window_end": now.isoformat(),
                  "hours": settings.hours, "limit_per_source": settings.limit,
                  "count": len(articles), "sources": {name: {"status": "not_requested", "exported": 0}
                                                       for name in SOURCES}}
        for result in results:
            report['sources'][result.source] = result.to_dict()
        storage.commit(articles, state, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    statuses = [r.status for r in results]
    return 1 if all(s == "error" for s in statuses) else (2 if any(s != "ok" for s in statuses) else 0)


def main():
    parser = argparse.ArgumentParser(description="New public news -> JSON; 48h/200 per source maximum")
    parser.add_argument('--sources', nargs='+', choices=SOURCES, default=list(SOURCES))
    parser.add_argument('--output', default=os.getenv('NEWS_OUTPUT', 'data/news'), help='Latest JSON batch and deduplication state')
    parser.add_argument('--hours', type=int, default=48)
    parser.add_argument('--limit', type=int, default=200)
    parser.add_argument('--timeout', type=float, default=20)
    parser.add_argument('--delay', type=float, default=0.5, help='Minimum interval between requests per host')
    parser.add_argument('--retries', type=int, default=2, help='Additional HTTP attempts')
    parser.add_argument('--max-pages', type=int, default=20)
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--log-level', choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'], default='INFO')
    args = parser.parse_args()
    args.sources = list(dict.fromkeys(args.sources))
    logging.basicConfig(level=args.log_level, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    logging.getLogger('httpx').setLevel(logging.WARNING)
    try:
        code = asyncio.run(run(args))
    except (ValueError, OSError, RuntimeError) as exc:
        logging.error('%s', exc)
        code = 1
    except KeyboardInterrupt:
        code = 130
    sys.exit(code)


if __name__ == '__main__':
    main()

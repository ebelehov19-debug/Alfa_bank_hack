from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import sys
from uuid import uuid4

from .models import Settings, SOURCES, UTC, source_cutoff, source_hours
from .sources import ADAPTERS
from .storage import Storage
from .transport import Transport


async def collect(settings, selected, state, now, transport):
    return await asyncio.gather(*(
        ADAPTERS[name](transport, settings).collect(
            settings.cutoff(now, name), now, {key for key in state if key.startswith(name + ":")})
        for name in selected
    ))


async def collect_to_directory(settings, output, selected=None):
    """Collect and durably save a batch using the same path as the CLI."""
    selected = list(SOURCES) if selected is None else list(selected)
    if not selected or any(name not in SOURCES for name in selected):
        raise ValueError("Select at least one supported news source")
    storage = Storage(output)
    now = datetime.now(UTC)
    with storage.lock():
        # A narrower query must not forget already exported articles: the next
        # run may restore the full 48h/7d window.
        state = storage.state({source: source_cutoff(source, now) for source in SOURCES})
        async with Transport(settings) as transport:
            results = await collect(settings, selected, state, now, transport)
        articles = [a for r in results for a in r.articles]
        for article in articles:
            state[article.id] = article.published_at.isoformat()
        report = {"schema_version": 1, "run_id": str(uuid4()),
                  "started_at": now.isoformat(), "finished_at": datetime.now(UTC).isoformat(),
                  "window_start": min(settings.cutoff(now, source) for source in selected).isoformat(),
                  "window_end": now.isoformat(),
                  "hours": max(source_hours(source, settings.hours) for source in selected),
                  "limit_per_source": settings.limit,
                  "count": len(articles), "sources": {name: {"status": "not_requested", "exported": 0,
                                                       "hours": source_hours(name, settings.hours),
                                                       "window_start": settings.cutoff(now, name).isoformat(),
                                                       "window_end": now.isoformat()}
                                                       for name in SOURCES}}
        for result in results:
            report['sources'][result.source].update(result.to_dict())
        storage.commit(articles, state, report)
    return report


async def run(args):
    settings = Settings(hours=args.hours, limit=args.limit, timeout=args.timeout,
                        delay=args.delay, retries=args.retries, max_pages=args.max_pages,
                        browser=not args.no_browser, browser_timeout=getattr(args, 'browser_timeout', 60),
                        browser_state=getattr(args, 'browser_state', None) or str(Path(args.output) / '.browser-state.json'))
    report = await collect_to_directory(settings, args.output, args.sources)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    statuses = [report['sources'][source]['status'] for source in args.sources]
    return 1 if all(s == "error" for s in statuses) else (2 if any(s != "ok" for s in statuses) else 0)


def main():
    parser = argparse.ArgumentParser(description="Public news -> JSON; MOEX/TASS 48h, Alfa 7 days; 200 per source maximum")
    parser.add_argument('--sources', nargs='+', choices=SOURCES, default=list(SOURCES))
    parser.add_argument('--output', default=os.getenv('NEWS_OUTPUT', 'data/news'), help='Latest JSON batch and deduplication state')
    parser.add_argument('--hours', type=int, default=48, help='MOEX/TASS window, 1..48 hours; Alfa always uses 7 days')
    parser.add_argument('--limit', type=int, default=200)
    parser.add_argument('--timeout', type=float, default=20)
    parser.add_argument('--browser-timeout', type=float, default=60, help='Maximum wait for visitor JavaScript checks')
    parser.add_argument('--browser-state', help='Playwright storage-state JSON from a previously verified visitor session')
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

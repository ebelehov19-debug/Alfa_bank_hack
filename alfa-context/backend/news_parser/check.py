import argparse
import json
from pathlib import Path

from .models import SOURCES, news_id, parse_date


def validate(directory):
    directory = Path(directory)
    if (directory / '.pending.json').exists():
        raise ValueError('Interrupted write: run the parser again to recover before reading JSON')
    report = json.loads((directory / 'run_report.json').read_text(encoding='utf-8'))
    combined = json.loads((directory / 'news.json').read_text(encoding='utf-8'))
    if not isinstance(combined, list):
        raise ValueError('news.json must contain a JSON array')
    seen, grouped = set(), {s: [] for s in SOURCES}
    start, end = parse_date(report.get('window_start')), parse_date(report.get('window_end'))
    if not start or not end or not 0 < (end - start).total_seconds() <= 48 * 3600:
        raise ValueError('Invalid report window')
    for row in combined:
        source = row.get('source')
        if source not in grouped or row.get('id') in seen:
            raise ValueError('Unknown source or duplicate ID')
        if row.get('id') != news_id(source, row.get('url', '')):
            raise ValueError('ID does not match source URL')
        published = parse_date(row.get('published_at'))
        collected = parse_date(row.get('collected_at'))
        if not published or not start <= published <= end or not collected:
            raise ValueError('Missing or out-of-window timestamp')
        if not row.get('title') or not isinstance(row.get('text'), str) or len(row['text']) < 40:
            raise ValueError('Missing title or full text')
        if not isinstance(row.get('categories'), list) or row.get('language') != 'ru':
            raise ValueError('Invalid categories or language')
        seen.add(row['id'])
        grouped[source].append(row)
    for source, rows in grouped.items():
        if len(rows) > min(200, report['limit_per_source']):
            raise ValueError(f'{source} exceeds the per-source limit')
        source_rows = json.loads((directory / f'{source}.json').read_text(encoding='utf-8'))
        if rows != source_rows or report['sources'][source]['exported'] != len(rows):
            raise ValueError(f'{source}.json differs from news.json or report')
    if len(combined) != report['count']:
        raise ValueError('Report count differs from news.json')
    return {'count': len(combined), 'sources': {s: report['sources'][s]['status'] for s in SOURCES}}


def main():
    parser = argparse.ArgumentParser(description='Check JSON structure, limits, timestamps and report consistency')
    parser.add_argument('directory')
    args = parser.parse_args()
    try:
        result = validate(args.directory)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        parser.exit(1, f'Validation failed: {exc}\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

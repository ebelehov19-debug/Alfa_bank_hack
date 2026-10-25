import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
from urllib.parse import urlsplit

from .models import SOURCES, UTC
from .storage import Storage, atomic_json

CHECK_DIRECTORIES = ('tass-check', 'tass-date-check', 'tass-next-check',
                     'tass-scroll-check', 'tass-full-check')


def cleanup(directory):
    directory = Path(directory)
    if not directory.is_dir():
        raise ValueError(f'Output directory does not exist: {directory}')
    removed, dropped = [], 0
    legacy = directory / 'e_disclosure.json'
    legacy_existed = legacy.exists() or legacy.is_symlink()
    storage = Storage(directory)
    with storage.lock():
        state_path = directory / 'state.json'
        state = storage.state(datetime.min.replace(tzinfo=UTC))
        if state_path.exists():
            previous = json.loads(state_path.read_text(encoding='utf-8'))
            dropped = len(previous['seen']) - len(state)
        # A complete pre-Alfa batch has two source files. Upgrade it on the
        # normal atomic storage path while retaining legacy disclosure cleanup.
        batch_names = ['moex.json', 'tass.json', 'news.json', 'run_report.json']
        present = [name for name in batch_names if (directory / name).exists()]
        if present and len(present) != len(batch_names):
            raise ValueError('Incomplete output batch; finish a regular parser run before cleanup')
        if present:
            rows = json.loads((directory / 'news.json').read_text(encoding='utf-8'))
            report = json.loads((directory / 'run_report.json').read_text(encoding='utf-8'))
            if 'alfa' in report['sources'] and not (directory / 'alfa.json').exists():
                raise ValueError('Incomplete output batch; missing alfa.json')
            rows = [row for row in rows if row['source'] in SOURCES]
            report['sources'] = {s: {**report['sources'].get(s, {'status': 'not_requested'}),
                                     'exported': sum(row['source'] == s for row in rows)}
                                 for s in SOURCES}
            report['count'] = len(rows)
            files = {f'{s}.json': [row for row in rows if row['source'] == s] for s in SOURCES}
            files.update({'news.json': rows,
                          'state.json': {'schema_version': 1, 'seen': state},
                          'run_report.json': report})
            atomic_json(directory / '.pending.json', files)
            storage.recover()
        elif state_path.exists() and dropped:
            atomic_json(state_path, {'schema_version': 1, 'seen': state})
        if legacy.exists() or legacy.is_symlink():
            legacy.unlink()
        if legacy_existed:
            removed.append(legacy.name)
        browser = directory / '.browser-state.json'
        if browser.exists():
            saved = json.loads(browser.read_text(encoding='utf-8'))
            def former_host(host):
                host = host.lower().lstrip('.')
                return host == 'e-disclosure.ru' or host.endswith('.e-disclosure.ru')
            cleaned = {**saved,
                       'cookies': [c for c in saved.get('cookies', [])
                                   if not former_host(c.get('domain', ''))],
                       'origins': [o for o in saved.get('origins', [])
                                   if not former_host(urlsplit(o.get('origin', '')).hostname or '')]}
            if cleaned != saved:
                atomic_json(browser, cleaned)
                if os.name != 'nt':
                    browser.chmod(0o600)
        for name in CHECK_DIRECTORIES:
            path = directory / name
            if path.is_symlink() or path.is_file():
                path.unlink()
                removed.append(name)
            elif path.is_dir():
                shutil.rmtree(path)
                removed.append(name)
    return {'removed': removed, 'discarded_state_ids': dropped,
            'sources': list(SOURCES)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', nargs='?', default='/data')
    args = parser.parse_args()
    try:
        print(json.dumps(cleanup(args.directory), ensure_ascii=False, indent=2))
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        parser.exit(1, f'Cleanup failed: {exc}\n')


if __name__ == '__main__':
    main()

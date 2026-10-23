from __future__ import annotations

from contextlib import contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import re

from .models import SOURCES, parse_date


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


class Storage:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def lock(self):
        handle = (self.directory / ".parser.lock").open("a+b")
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            try:
                if handle.read(1) == b"":
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                handle.close()
                raise RuntimeError("Another parser is using this output directory") from None
        else:
            import fcntl
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                handle.close()
                raise RuntimeError("Another parser is using this output directory") from None
        try:
            self.recover()
            yield self
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            handle.close()

    def state(self, cutoff):
        path = self.directory / "state.json"
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("schema_version") != 1 or not isinstance(data.get("seen"), dict):
            raise ValueError("Unrecognized state.json; refusing to silently reset deduplication")
        result = {}
        for key, value in data['seen'].items():
            if not key.startswith(tuple(source + ':' for source in SOURCES)):
                continue
            date = parse_date(value)
            if not date:
                raise ValueError("Invalid publication timestamp in state.json")
            if date >= cutoff:
                result[key] = value
        return result

    def commit(self, articles, state, report):
        flat = [a.to_dict() for a in articles]
        flat.sort(key=lambda a: (a['published_at'], a['id']), reverse=True)
        files = {f"{source}.json": [a for a in flat if a['source'] == source] for source in SOURCES}
        files['news.json'] = flat
        files['state.json'] = {"schema_version": 1, "seen": state}
        files['run_report.json'] = report
        atomic_json(self.directory / ".pending.json", files)
        self.recover()

    def recover(self):
        pending = self.directory / ".pending.json"
        if not pending.exists():
            return
        files = json.loads(pending.read_text(encoding="utf-8"))
        expected = {*(f"{s}.json" for s in SOURCES), "news.json", "state.json", "run_report.json"}
        if set(files) == expected | {'e_disclosure.json'}:
            files.pop('e_disclosure.json')
            files['news.json'] = [row for row in files['news.json'] if row['source'] in SOURCES]
            files['state.json']['seen'] = {k: v for k, v in files['state.json']['seen'].items()
                                            if k.startswith(tuple(s + ':' for s in SOURCES))}
            report = files['run_report.json']
            report['sources'] = {s: report['sources'][s] for s in SOURCES}
            report['count'] = len(files['news.json'])
        if set(files) != expected:
            raise ValueError("Invalid pending transaction")
        # Preserve a pre-upgrade latest batch as well as the incoming batch. Archive
        # persistence precedes any replacement of latest files or deduplication state.
        previous_news = self.directory / 'news.json'
        previous_report = self.directory / 'run_report.json'
        if previous_news.exists() and previous_report.exists():
            self.archive(json.loads(previous_news.read_text(encoding='utf-8')),
                         json.loads(previous_report.read_text(encoding='utf-8')))
        self.archive(files['news.json'], files['run_report.json'])
        for name in [*(f"{s}.json" for s in SOURCES), "news.json", "state.json", "run_report.json"]:
            atomic_json(self.directory / name, files[name])
        pending.unlink()
        (self.directory / 'e_disclosure.json').unlink(missing_ok=True)

    def archive(self, articles, report):
        if not articles:
            return
        if not isinstance(articles, list) or not isinstance(report, dict):
            raise ValueError('Invalid batch for archive')
        articles = [row for row in articles if isinstance(row, dict) and row.get('source') in SOURCES]
        if not articles:
            return
        payload = {'articles': articles, 'report': report}
        fingerprint = sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()[:24]
        run_id = str(report.get('run_id', ''))
        safe = re.fullmatch(r'[A-Za-z0-9_-]{1,100}', run_id) and not re.fullmatch(
            r'CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]', run_id, re.I)
        name = run_id if safe else 'batch-' + fingerprint
        path = self.directory / 'batches' / (name + '.json')
        if path.exists():
            if json.loads(path.read_text(encoding='utf-8')) == payload:
                return
            path = path.with_name(name + '-' + fingerprint + '.json')
            if path.exists() and json.loads(path.read_text(encoding='utf-8')) == payload:
                return
        atomic_json(path, payload)

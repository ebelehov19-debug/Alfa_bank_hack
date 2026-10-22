from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path

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
            if handle.read(1) == b"":
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
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
        if set(files) != expected:
            raise ValueError("Invalid pending transaction")
        for name in [*(f"{s}.json" for s in SOURCES), "news.json", "state.json", "run_report.json"]:
            atomic_json(self.directory / name, files[name])
        pending.unlink()

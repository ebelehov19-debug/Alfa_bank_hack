"""One-command localhost web app + supervised llama.cpp CPU process."""
import argparse
import json
import signal
import socket
import subprocess
import sys
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from relevance import Busy, Classifier, MODEL, ModelUnavailable, THRESHOLD

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "frontend" / "dist"


def load_companies():
    text = (STATIC / "data.js").read_text(encoding="utf-8")
    return {c["company_id"]: c for c in json.loads(text.split("=", 1)[1].strip().rstrip(";"))}


def validate_request(data, companies):
    if not isinstance(data, dict):
        raise ValueError("Ожидается JSON-объект.")
    uid, role = data.get("company_id"), data.get("role", "cfo")
    if not isinstance(uid, str) or uid not in companies:
        raise ValueError("Неизвестная компания.")
    if role not in ("cfo", "ceo", "treasury"):
        raise ValueError("Неизвестная роль.")
    title, body = data.get("title", ""), data.get("text", "")
    if not isinstance(title, str) or not isinstance(body, str):
        raise ValueError("Заголовок и текст должны быть строками.")
    title, body = title.strip(), body.strip()
    if not title or len(title) > 200 or not body or len(body) > 2500:
        raise ValueError("Нужны заголовок (1–200 символов) и текст (1–2500 символов).")
    return companies[uid], role, title, body


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, classifier, companies, **kwargs):
        self.classifier, self.companies = classifier, companies
        super().__init__(*args, directory=str(STATIC), **kwargs)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def reply(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def local_request(self):
        allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin")
        return host in allowed and (not origin or origin in {"http://" + h for h in allowed})

    def do_GET(self):
        if not self.local_request():
            return self.reply(403, {"error": "Только локальный доступ."})
        path = urlsplit(self.path).path
        if path == "/api/health":
            ready = self.classifier.health()
            return self.reply(200, {"ready": ready, "model": MODEL, "device": "CPU",
                                    "threshold": THRESHOLD})
        if path not in ("/", "/index.html", "/app.js", "/local-ai.js", "/data.js", "/style.css"):
            return self.reply(404, {"error": "Не найдено."})
        return super().do_GET()

    def do_HEAD(self):
        if not self.local_request() or urlsplit(self.path).path not in ("/", "/index.html", "/app.js", "/local-ai.js", "/data.js", "/style.css"):
            self.send_error(404)
            return
        return super().do_HEAD()

    def do_POST(self):
        if not self.local_request():
            return self.reply(403, {"error": "Только локальный доступ."})
        if self.path != "/api/relevance":
            return self.reply(404, {"error": "Не найдено."})
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            return self.reply(415, {"error": "Нужен Content-Type: application/json."})
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size <= 0 or size > 16384:
                return self.reply(413, {"error": "Неверный размер запроса."})
            data = json.loads(self.rfile.read(size))
            args = validate_request(data, self.companies)
            result = self.classifier.classify(*args)
            self.reply(200, result)
        except (ValueError, UnicodeDecodeError) as exc:
            self.reply(400, {"error": str(exc)})
        except Busy as exc:
            self.reply(429, {"error": str(exc)})
        except ModelUnavailable as exc:
            self.reply(503, {"error": str(exc)})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--model-port", type=int, default=8091)
    parser.add_argument("--threads", type=int, default=4)
    args = parser.parse_args()
    if not 1 <= args.threads <= 16:
        parser.error("--threads должен быть от 1 до 16")
    model = ROOT / ".local/models/qwen2.5-1.5b-instruct-q4_k_m.gguf"
    binaries = list((ROOT / ".local/runtime").rglob("llama-server"))
    if not model.is_file() or not binaries:
        sys.exit("Модель ещё не установлена. Выполните: bash setup-local.sh")
    classifier = Classifier(f"http://127.0.0.1:{args.model_port}")
    handler = partial(Handler, classifier=classifier, companies=load_companies())
    try:
        web = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    except OSError:
        sys.exit(f"Порт {args.port} занят. Остановите прежний сервер или добавьте --port 8082.")
    with socket.socket() as probe:
        probe.settimeout(1)
        if probe.connect_ex(("127.0.0.1", args.model_port)) == 0:
            web.server_close()
            sys.exit(f"Порт модели {args.model_port} занят. Используйте --model-port 8092.")
    log = (ROOT / ".local/model.log").open("a")
    process = None
    def stop(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    try:
        process = subprocess.Popen([
            str(binaries[0]), "-m", str(model), "--host", "127.0.0.1",
            "--port", str(args.model_port), "-ngl", "0",
            "-t", str(args.threads), "-tb", str(args.threads),
            "-c", "3072", "-np", "1", "-b", "256", "-ub", "128",
        ], stdout=log, stderr=log)
        print("Загрузка Qwen2.5 1.5B на CPU… Лог: .local/model.log", flush=True)
        for _ in range(120):
            if process.poll() is not None:
                raise RuntimeError("Движок завершился. Подробности: .local/model.log")
            if classifier.health():
                break
            time.sleep(1)
        else:
            raise RuntimeError("Модель не загрузилась за 120 секунд. См. .local/model.log")
        print(f"ГОТОВО: http://localhost:{args.port} | CPU, {args.threads} потока | Ctrl+C — остановить", flush=True)
        web.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановка сайта и модели…", flush=True)
    except (RuntimeError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        web.server_close()
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        log.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

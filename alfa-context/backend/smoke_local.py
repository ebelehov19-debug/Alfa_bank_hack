"""Live CPU smoke test. Run while run-local.sh is running."""
import argparse
import json
import time
from urllib.request import Request, urlopen

CASES = [
    ("direct", "Новые требования к производству металлопрофиля",
     "С 1 ноября все производители профилей с помощью холодной штамповки или гибки обязаны проводить дополнительный контроль качества продукции.", True),
    ("unrelated", "Результат футбольного матча",
     "Футбольный клуб выиграл товарищеский матч со счётом 2:0. Два гола забил нападающий.", False),
    ("missing", "Новая программа поддержки",
     "Объявлена программа поддержки бизнеса. Регион, отрасли, условия участия и сроки пока не опубликованы.", False),
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    base = f"http://127.0.0.1:{args.port}"
    with urlopen(base + "/api/health", timeout=5) as r:
        health = json.load(r)
    assert health["ready"], health
    print("HEALTH:", health)
    failures = 0
    results = []
    for name, title, body, expected in CASES:
        payload = {"company_id": "U_23929", "role": "cfo", "title": title, "text": body}
        started = time.monotonic()
        request = Request(base + "/api/relevance", json.dumps(payload).encode(),
                          {"Content-Type": "application/json"})
        with urlopen(request, timeout=190) as r:
            result = json.load(r)
        passed = result["relevant"] is expected
        failures += not passed
        results.append({"case": name, **result})
        print(json.dumps({"case": name, "passed": passed, "wall_seconds": round(time.monotonic()-started, 2), **result}, ensure_ascii=False))
        # Identical request must hit the cache.
        with urlopen(request, timeout=5) as r:
            assert json.load(r)["cached"]
    print(f"Passed {len(CASES)-failures}/{len(CASES)}. Small smoke set, not a quality benchmark.")
    raise SystemExit(bool(failures))


if __name__ == "__main__":
    main()

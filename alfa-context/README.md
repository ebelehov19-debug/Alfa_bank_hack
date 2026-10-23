# Альфа Контекст — весь текущий проект

backend/ — ядро анализа CSV, сборщик новостей и тесты.
frontend/ — рабочий фронт, HTML/CSS/JS.
data/ — исходные CSV.
docs/ — задание хакатона.

Это прототип: фронт использует срез трёх компаний; новости и выводы
демонстрационные. API и LLM ещё не подключены. Отдельный парсер новостей
реализован в backend/news_parser; к фронту его выдача пока не подключена.
Ключей в архиве нет. Фронт автоматически не запускается.

## Сбор новостей через Docker

Из этой папки:

```bash
docker compose build parser
docker compose run --rm parser
docker compose --profile test build parser-test
docker compose --profile test run --rm parser-test
```

Результат: `data/news/news.json`, `data/news/moex.json` и `data/news/tass.json`, только новые
публикации за 48 часов, до 200 на источник. Подробности и проверка результата:
[backend/README_NEWS.md](backend/README_NEWS.md).

## Запуск ядра (Python 3.12+, без внешних зависимостей)
Из корня проекта:
python backend/core.py --db context.sqlite import data
python backend/core.py --db context.sqlite profile U_23929

Для тестов перейдите в backend и выполните:
python -m unittest discover -s tests -v

## Ручной запуск фронта
Из корня проекта:
python -m http.server 8080 --directory frontend/dist --bind 127.0.0.1
Открыть http://localhost:8080 . Остановить Ctrl+C.

SQLite-база восстанавливается импортом CSV и в архив не включена.

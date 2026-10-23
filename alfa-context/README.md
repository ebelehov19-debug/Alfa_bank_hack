# Альфа Контекст — весь текущий проект

backend/ — ядро анализа CSV, сборщик новостей и тесты.
frontend/ — рабочий фронт, HTML/CSS/JS.
data/ — исходные CSV.
docs/ — задание хакатона.

Фронт получает новости через локальный API. Gemma 4 26B A4B отбирает
новости по деловой тематике, GLM-5.3-Flash анализирует их с учётом компании
и роли пользователя. Ключ подключается через `.env` только на сервере.
До настройки ключа доступны профили, граф и ранее сохранённые результаты.
Настройка, запуск и проверка: [backend/README_AI.md](backend/README_AI.md).

Кнопка «Анализировать новости» сначала запускает парсер MOEX и ТАСС,
сохраняет публикации в `data/news/`, затем читает `news.json` и архив
`batches/` и передаёт тексты моделям. Результаты AI сохраняются в
`data/ai.sqlite`. В интерфейсе видны этапы сбора и анализа; запуск доступен
и при пустой папке новостей. API-образ Docker включает Chromium.

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
python backend/core.py --db data/context.sqlite import data
python backend/core.py --db data/context.sqlite profile U_23929

Для тестов перейдите в backend и выполните:
python -m unittest discover -s tests -p test_core.py -v

## Запуск фронта и API
Из `alfa-context/backend`:
`python -m news_api`
Открыть http://127.0.0.1:8080 . Остановить Ctrl+C.
Зависимости: `python -m pip install -r requirements-api.txt` и
`python -m playwright install chromium`. Docker: `docker compose up --build api`
из `alfa-context`.

SQLite-база восстанавливается импортом CSV и в архив не включена.

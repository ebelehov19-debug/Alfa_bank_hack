# Альфа Контекст — весь текущий проект

backend/ — ядро анализа CSV и тесты.
frontend/ — рабочий фронт, HTML/CSS/JS.
data/ — исходные CSV.
docs/ — задание хакатона.

Это прототип: фронт использует срез трёх компаний; новости и выводы
демонстрационные. API, LLM и сбор новостей ещё не подключены.
Ключей в архиве нет. Фронт автоматически не запускается.

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

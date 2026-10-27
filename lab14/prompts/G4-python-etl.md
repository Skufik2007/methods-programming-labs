# G4 — аналитический конвейер на Python: Polars, Parquet, DuckDB, графики, клиент Arrow (задание 3 и основа отчёта)

Модель-генератор: Claude Sonnet 5.5, effort medium (подагент, не видит ход работы).

## Промпт

Ты — инженер данных на Python. Прочитай контракт
`D:\6 семестр\Методы программирования\lab14\docs\ARCHITECTURE.md`, разделы 2, 6, 9, 12, 13.
Сделай аналитическую часть конвейера.

### Что сделать

1. **`pyproject.toml` в корне** (setuptools, пакет `vitals` из каталога `python/`, Python 3.12):
   зависимости `polars`, `duckdb`, `pyarrow`, `numpy`, `plotly`, `matplotlib`, `nats-py`,
   `fastapi`, `uvicorn[standard]`, `httpx`, `aiohttp`, `psutil`, `etcd3-py` (или другой
   etcd-клиент, совместимый с etcd v3 gRPC-gateway — выбери рабочий для Python 3.12 и
   обоснуй); dev: `pytest`, `pytest-asyncio`, `pytest-cov`, `ruff`. Настройки ruff
   (E,F,W,I,UP,B,S,ASYNC, line-length 120) и pytest (`asyncio_mode = "auto"`,
   `testpaths = ["tests"]`) как в `D:\6 семестр\Методы программирования\lab13\pyproject.toml`.
   Установи пакет в существующий `.venv` (`pip install -e ".[dev]"`; если пакеты ещё
   ставятся в фоне — подожди).
2. **`python/vitals/analytics/`** — шаги раздела 9, каждый шаг — функция над
   `polars.LazyFrame`/`DataFrame` без ввода-вывода (тестируемо), плюс тонкий CLI
   `python -m vitals.analytics run --source files|flight --in <dir> --out <dir>
   [--flight grpc://host:port ...]`:
   - загрузка JSONL (`scan_ndjson` по `raw/*.jsonl`, `agg/*.jsonl`) и Arrow Flight
     (`pyarrow.flight`, тикет из контракта, несколько серверов → `concat`), сводка:
     `head(5)`, схема, число строк, пропуски по столбцам;
   - очистка с **отчётом по шагам** (список `{"step", "rows_before", "rows_after",
     "affected", "note"}` → `out/cleaning_report.json`): дубликаты по `(device_id, seq)`,
     нормализация `unit`, `ts` из будущего (относительно максимального `ts` набора + допуск),
     валидация через `vitals.validation.validate` (её пишет другой генератор: импортируй
     `from vitals.validation import validate`; если модуля ещё нет — сделай заглушку-протокол
     в своём коде не создавая этот файл), пропуски, приведение типов;
   - агрегаты по `(patient_id, metric)`: SUM, AVG, MIN, MAX, COUNT + почасовой профиль +
     поиск эпизодов по правилам раздела 7 (по минутным средним) → `out/*.parquet`;
   - Parquet: `out/clean/` с партиционированием по `metric`, `out/agg.parquet`;
   - DuckDB: 3–4 SQL-запроса к Parquet (фильтр, группировка, сортировка, оконная функция —
     например, скользящее среднее ЧСС), **тот же анализ на Polars**, замер времени обоих
     (несколько прогонов, медиана) → `out/timings.json`; совпадение результатов
     проверяй в коде;
   - графики (раздел 9.6): Plotly HTML + PNG через matplotlib → `out/img/`.
3. **`python/vitals/common/`** — логи (JSON-строки, UTF-8 на stderr даже на Windows —
   `reconfigure(encoding="utf-8")`), настройки из окружения (без чтения при импорте).

### Ограничения

- Тесты не пиши (другая модель). Шаги — чистые функции.
- Данные для проверки: собери Go-бинарники `go build -o go/bin/ ./go/cmd/...` (Go в
  `D:\dev\go\bin`), запусти `sensorsim` (`DEVICES=50 PERIOD_MS=200` — ускоренно) и
  `collector` (`-duration 60s`, `OUT_DIR` во временном каталоге), затем свой CLI по
  `--source files`. Flight-сервер в сборщике делает другая модель параллельно — если к
  твоему финалу в `go/internal/sinks/flightsink` его ещё нет, проверь клиент Flight только
  кодом, без запуска, и напиши об этом.
- Если что-то не работает после **двух** попыток — не продолжай отлаживать, опиши в ответе.
- Не трогай `go/`, `rust/`, `python/vitals/validation.py`. Не делай git-коммитов. Файлы
  создавай инструментом Write. Комментарии на русском, к неочевидному.

### Ответ

Коротко: файлы, отчёт очистки (сколько строк на каждом шаге), времена Polars и DuckDB,
список графиков, допущения.

## Почему так

- Шаги — чистые функции: тестируются без файлов, а CLI остаётся тонким.
- Отчёт очистки с числами по шагам — требование «документировать каждый шаг» и материал
  для отчёта.
- Полный прогон конвейера в конце — генератор проверяет свой код на настоящих данных из
  эмулятора, а не на выдуманных.

## Ответ генератора (итоговое сообщение, сокращено)

Прогон генератором на данных эмулятора (`DEVICES=50 PERIOD_MS=200`, сборщик 60 с, `--source files`),
ruff чисто; клиент Arrow Flight написан, но не запускался (Flight-сервер ещё в работе).

Файлы: `pyproject.toml` (setuptools, `where=["python"]`), `python/vitals/common/` (логи JSON в UTF-8,
`Settings.from_env()`), `python/vitals/analytics/` — `schema`, `load` (`scan_jsonl`, `fetch_flight`,
`summarize`), `clean`, `aggregate`, `queries` (DuckDB и Polars с замером), `plots`, `pipeline`, `cli`.

Отчёт очистки: 109 637 → 104 151 строк — дубликаты 113, единицы исправлены 2163, будущие `ts` 1086,
вне диапазона 2117, пропуски 2170, `battery` заполнено 1049.
Времена (медиана 5 прогонов, мс, DuckDB / Polars): filter 4,7 / 8,5; group by 19,8 / 38,3;
top-10 12,9 / 14,3; скользящее среднее 30,8 / 32,9; результаты совпадают (проверка в коде).
Эпизодов 7 (fever 2, desaturation 3, hypertension 1, tachycardia 1). Графики HTML + PNG:
timeseries, hist_heart_rate, heatmap_heart_rate, defects.

Допущения генератора: `coerce_types` первым шагом; будущий `ts` — сравнение с минимальным `ts` более
поздних `seq` устройства (максимум набора сам оказывается будущим); `validate` удаляет коды 2–4, код 1
оставлен шагу `drop_missing`; нормализация единицы — регистр, пробелы, пустая; эпизоды по минутным
данным, `sensor_fault` после очистки не считается; etcd-клиент `etcd3-py` (HTTP gRPC-gateway);
`tzdata` на Windows (Polars падал на datetime с UTC).

# G3 — Rust-валидатор с PyO3 (задание 4)

Модель-генератор: Claude Sonnet 5.5, effort medium (подагент, не видит ход работы).

## Промпт

Ты — опытный Rust-разработчик. Прочитай контракт
`D:\6 семестр\Методы программирования\lab14\docs\ARCHITECTURE.md`, разделы 2 и 10.
Напиши библиотеку валидации показаний датчиков на Rust и встрои её в Python через PyO3.

### Что сделать

1. Крейт `rust/vitals_validate` (PyO3 + `numpy` crate, сборка maturin, модуль
   `vitals_validate`, abi3 для Python 3.12):
   - `validate_columns(metric, value, unit) -> numpy.ndarray[uint8]` по разделу 10:
     `metric` и `unit` — списки строк (или `None`), `value` — `numpy.float64` массив (NaN =
     пропуск). Коды 0–4, приоритет при нескольких нарушениях: 3 → 1 → 2 → 4 (сначала
     неизвестная метрика, потом пропуск, потом диапазон, потом единица);
   - единица валидна только точным совпадением с таблицей раздела 2;
   - `ranges() -> dict[str, tuple[float, float, str]]` — диапазоны и единицы;
   - разные длины входов → `ValueError` с понятным текстом;
   - тяжёлый цикл без GIL (`py.allow_threads`), без лишних копий `value` (readonly view).
   - Rust-тесты (`cargo test`) на чистую функцию валидации одной строки и на граничные
     значения диапазонов (границы включительно).
2. Python-обёртка `python/vitals/validation.py` с тем же контрактом:
   `validate(df: polars.DataFrame) -> polars.Series[uint8]` — использует Rust-модуль, если он
   собран, иначе **чистую реализацию на Polars-выражениях** (`validate_polars`), которая
   доступна и явно (для сравнения скорости); константы диапазонов в Python берутся из
   `ranges()` модуля, а без модуля — из той же таблицы (одна таблица в Python-файле с
   комментарием, что она должна совпадать с Rust; совпадение проверит тест).
   `python/vitals/__init__.py` создай пустым, если его нет.
3. `rust/vitals_validate/README.md` — как собрать (`maturin develop --release` в `.venv`).

### Ограничения

- Rust: `RUSTUP_HOME=D:\dev\rustup`, `CARGO_HOME=D:\dev\cargo` (добавь `D:\dev\cargo\bin` в
  PATH); Python — `.venv` в корне репозитория (если ещё ставятся пакеты — подожди, не
  создавай второй venv); для PyO3 задай `PYO3_PYTHON` на `.venv\Scripts\python.exe`.
  Собери `maturin develop --release`, запусти `cargo test` и короткую проверку из Python:
  совпадение Rust и Polars на 100 000 случайных строк с дефектами и время обеих реализаций
  на 1 млн строк (выведи числа).
- После **двух** неудачных попыток сборки — остановись и опиши проблему.
- Не трогай `go/`, `pyproject.toml` (его создаёт другой генератор). Не делай git-коммитов.
  Файлы создавай инструментом Write. Комментарии на русском, к неочевидному.

### Ответ

Коротко: файлы, результаты `cargo test`, совпадение реализаций, время Rust и Polars на 1 млн строк.

## Почему так

- PyO3, а не cgo: проверенный путь из ЛР №10 (maturin уже установлен), и валидация нужна на
  шаге очистки в Python.
- Запасная реализация на Polars с тем же контрактом: честное сравнение скорости и работа
  без собранного модуля; совпадение результатов — защита от расхождения таблиц.

## Ответ генератора (итоговое сообщение, сокращено)

Файлы: `rust/vitals_validate/` (`Cargo.toml` — pyo3 0.25 abi3-py312 + numpy 0.25, cdylib+rlib;
`pyproject.toml` maturin; `src/lib.rs` — `validate_row` с приоритетом 3→1→2→4, `validate_columns`
с readonly view и `allow_threads`, `ranges()`; README), `python/vitals/__init__.py`,
`python/vitals/validation.py` (`validate`, `validate_rust`, `validate_polars`, `has_rust`, `RANGES`).

Результаты генератора: `cargo test` 8/8; `maturin develop --release` собран (после `PYTHONUTF8=1`:
maturin падал на кириллице в пути); Rust и Polars совпали на 100 000 строк с дефектами (0 расхождений).
Время на 1 млн строк (лучшее из 3): Rust через обёртку 0,236 с, Polars 0,128 с, только ядро Rust 0,104 с —
`to_list()` строковых столбцов стоит ~0,13 с, поэтому Rust с обёрткой проигрывает.

Замечания генератора: нужны `PYO3_PYTHON` и `VIRTUAL_ENV`; null-метрика → код 3, null-единица → код 4;
теста совпадения таблиц в pytest нет (проверено одноразовым скриптом).

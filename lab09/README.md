Четвериков Михаил Михайлович, группа 221331, вариант 3, лабораторная №9

# Лабораторная работа №9

**Мультиязычное программирование: Go для сетевых сервисов, Rust для производительности**

- **Дисциплина:** Методы программирования
- **Уровень сложности:** повышенный (выполнены задания средней и повышенной сложности)

**Цель:** освоить интеграцию Python с Go (для микросервисов) и Rust (для высокопроизводительных библиотек) в рамках одного проекта.

## Задания варианта 3

| Колонка таблицы | № задания | Формулировка | Реализация |
|---|---|---|---|
| Средн. 1 (Go) | 3 | Скомпилировать Go-программу в бинарь и вызвать её из Python (subprocess) | [`go/cmd/calculator`](go/cmd/calculator), [`python/go_subprocess.py`](python/go_subprocess.py) |
| Средн. 2 (Go) | 5 | Реализовать на Go простой TCP-сервер, к которому подключается Python-клиент | [`go/cmd/tcpserver`](go/cmd/tcpserver), [`python/tcp_client.py`](python/tcp_client.py) |
| Средн. 3 (Rust) | 8 | Собрать модуль с Maturin и импортировать в Python | [`rust/fastmath`](rust/fastmath), [`python/rust_module.py`](python/rust_module.py) |
| Повыш. 1 | 3 | Настроить сборку Rust-модуля в CI/CD и публикацию на PyPI | [`.github/workflows/release.yml`](.github/workflows/release.yml) |
| Повыш. 2 | 1 | Создать микросервис на Go для тяжёлых вычислений и вызывать его из Python (HTTP) | [`go/cmd/microservice`](go/cmd/microservice), [`python/microservice_client.py`](python/microservice_client.py) |

## Структура проекта

```
lab9/
├── go/                         # Go-модуль (go 1.22+)
│   └── cmd/
│       ├── calculator/         # задание 3: JSON stdin -> stdout
│       ├── tcpserver/          # задание 5: TCP-сервер, горутина на соединение
│       └── microservice/       # повыш. 1: HTTP-сервис вычислений
├── rust/fastmath/              # задание 8: Python-модуль на Rust (PyO3 + Maturin)
│   ├── Cargo.toml
│   ├── pyproject.toml
│   └── src/lib.rs
├── python/                     # Python-часть: клиенты и демонстрации
├── tests/test_integration.py   # интеграционные тесты Python <-> Go <-> Rust
└── .github/workflows/
    ├── ci.yml                  # тесты Go, Rust и Python на Linux и Windows
    └── release.yml             # повыш. 3: колёса + публикация на PyPI
```

## Требования

- Python 3.9+
- Go 1.22+ — https://go.dev/dl/
- Rust (stable) — https://rustup.rs/; на Windows нужны MSVC Build Tools и Windows SDK
- Maturin: `pip install maturin`

## Сборка и запуск

```bash
# 1. Rust-модуль: сборка Maturin и установка в текущий Python
cd rust/fastmath
maturin develop --release      # или готовое колесо с PyPI: pip install fastmath-chetverikov
cd ../..

# 2. Демонстрации (Go-программы собираются автоматически в bin/ при первом запуске)
python python/go_subprocess.py
python python/tcp_client.py
python python/rust_module.py
python python/microservice_client.py

# 3. Тесты
cd go && go test ./... && cd ..
cd rust/fastmath && cargo test && cd ../..
python -m unittest discover -s tests -v
```

Go-серверы можно запустить и отдельно:

```bash
cd go
go run ./cmd/tcpserver -addr 127.0.0.1:9000
go run ./cmd/microservice -addr 127.0.0.1:8080
curl "http://127.0.0.1:8080/primes?limit=10000000"
curl -X POST http://127.0.0.1:8080/matmul -d '{"size":300,"seed":1}'
```

## Описание решения

### Задание 3. Go-бинарь, вызываемый через subprocess

`calculator` читает из stdin JSON `{"numbers": [...]}` и пишет в stdout статистику: количество, сумму, сумму квадратов, минимум, максимум и среднее. Переполнение `int64` проверяется явно. При ошибке программа выводит `{"error": "..."}` и завершается с кодом 1.

Ошибка пишется в stdout, а не в stderr, намеренно: так у вызывающей стороны один канал с машиночитаемым JSON, а код выхода показывает, успех это или ошибка.

На стороне Python функция `calculate()` компилирует бинарь через `go build` (один раз за процесс; что пересобирать, Go решает сам по своему кэшу), вызывает его через `subprocess.run` с тайм-аутом и превращает ошибку Go в исключение `CalculatorError`.

### Задание 5. TCP-сервер на Go и Python-клиент

Протокол построчный: одна JSON-строка на запрос, одна на ответ. Команды: `ping`, `upper`, `reverse`, `sum`, `time`, `quit`. Особенности сервера:

- каждое соединение обслуживается в отдельной **горутине**;
- длина строки ограничена 1 МБ, неактивные соединения закрываются по тайм-ауту чтения;
- на запись ответа стоит write deadline, поэтому клиент, который не читает ответы, не подвешивает горутину;
- при SIGINT/SIGTERM сервер корректно закрывает слушатель и все соединения и ждёт завершения горутин;
- ошибочная команда не разрывает соединение.

`TcpClient` на Python — контекстный менеджер поверх `socket`. Тест `test_many_clients` проверяет 20 одновременных клиентов.

### Задание 8. Rust-модуль через PyO3 + Maturin

Модуль `fastmath` (дистрибутив `fastmath-chetverikov`):

| Функция | Описание |
|---|---|
| `sum_squares(list[int]) -> int` | сумма квадратов; при переполнении бросает `OverflowError` |
| `count_primes(limit) -> int` | количество простых ≤ limit (решето Эратосфена по нечётным числам, 1 бит на число; для limit = 1e9 около 60 МБ) |
| `primes(limit) -> list[int]` | список простых ≤ limit |
| `is_prime(n) -> bool` | детерминированный тест Миллера — Рабина для всего диапазона u64 |

Вычисления написаны как обычные Rust-функции и покрыты `cargo test`. Обёртки `#[pyfunction]` только конвертируют типы и ошибки. Тяжёлые функции отпускают GIL (`allow_threads`). Модуль собирается как **abi3**: одно колесо подходит для Python 3.9 и новее.

### Повышенное 1. Go-микросервис вычислений

| Метод | Путь | Что делает |
|---|---|---|
| GET | `/health` | проверка готовности |
| GET | `/primes?limit=N&workers=W` | количество простых ≤ N, параллельное сегментированное решето |
| POST | `/matmul` `{"size":N,"seed":S}` | перемножение случайных матриц N×N, ответ — след матрицы |

Вычисления распределяются по горутинам через каналы. Число одновременных тяжёлых задач ограничено семафором (флаг `-max-jobs`). Все параметры проверяются (400 при ошибке), есть тайм-ауты и graceful shutdown. Клиент `ComputeClient` использует только стандартную библиотеку (`urllib`), параллельные запросы отправляются через `ThreadPoolExecutor`.

Поле `elapsed_ms` в ответе — это чистое время вычисления. Оно измеряется после того, как запрос получил слот семафора, поэтому ожидание в очереди и сетевые задержки в него не входят.

### Повышенное 3. CI/CD и публикация на PyPI

- **`ci.yml`** — на каждый push и PR на Ubuntu и Windows: линтеры (`gofmt`, `go vet`, `rustfmt`, `clippy -D warnings`), `go test -race`, `cargo test`, сборка колеса Maturin, установка и интеграционные тесты Python.
- **`release.yml`** — перед публикацией сверяет версию тега с `pyproject.toml` и `Cargo.toml`. Затем собирает колёса для Linux (manylinux), Windows и macOS через `PyO3/maturin-action`, проверка установки колеса и сборка sdist. При push тега `v*` артефакты публикуются на PyPI через **Trusted Publishing** (OIDC), поэтому API-токен не хранится в секретах репозитория.

**Пакет опубликован:** https://pypi.org/project/fastmath-chetverikov/ (версия 0.1.0 выпущена тегом `v0.1.0`). На PyPI лежат колёса для Linux x86_64, Windows x64 и macOS arm64, а также sdist.

```bash
pip install fastmath-chetverikov
python -c "import fastmath; print(fastmath.sum_squares([1, 2, 3, 4, 5]))"   # 55
```

Как выпустить новую версию:

1. Однократно на https://pypi.org/manage/account/publishing/ добавлен доверенный издатель: проект `fastmath-chetverikov`, этот репозиторий, workflow `release.yml`, environment `pypi`.
2. Поднять версию в `rust/fastmath/pyproject.toml` и `Cargo.toml`, затем выполнить `git tag vX.Y.Z && git push origin vX.Y.Z`. GitHub Actions соберёт колёса и опубликует их.

## Результаты

Запуск на Windows 11, 12 логических ядер, Go 1.27, Rust 1.99, Python 3.12.

```
===== go_subprocess
Python -> Go: [1, 2, 3, 4, 5]
Go -> Python: {'count': 5, 'sum': 15, 'sum_squares': 55, 'min': 1, 'max': 5, 'mean': 3}
Ошибка обработана корректно: список чисел пуст

===== tcp_client
ping    -> pong
upper   -> ПРИВЕТ ИЗ PYTHON
reverse -> nohtyP и oG
sum     -> 60
ошибка  -> неизвестная команда "unknown"

===== rust_module
sum_squares([1..5]) = 55
is_prime(1_000_000_007) = True
count_primes(10_000_000) = 664579
  Rust:        8.9 мс
  Python:     78.7 мс  (ускорение x8.9)

===== microservice_client
простых до 50 млн: 3001134 за 17.202 мс (12 горутин)
матрицы 500x500: след = 62482.912 за 20.435 мс
4 параллельных запроса за 38 мс
Ошибка валидации обработана: HTTP 400: size должен быть в диапазоне 1..1500
```

Тесты: Go — 3 пакета, Rust — 3 теста, Python (интеграционные) — 12 тестов. Все проходят.

Python-версия решета для сравнения тоже векторизована: присваивание срезу `bytearray` выполняется на C. Rust всё равно быстрее почти в 9 раз, потому что хранит только нечётные числа по 1 биту и считает простые через `popcount`. Многопоточный Go-сервис считает простые до 50 млн примерно за 17 мс благодаря сегментированному решету, которое помещается в кэш процессора, и параллельной работе горутин.


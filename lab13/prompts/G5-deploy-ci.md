# G5 — Docker Compose, CI и сквозная проверка (задания 1, 3, 4, 5 — инфраструктура)

Модель-генератор: Claude Sonnet 5.5 (подагент, не видит ход работы).

## Промпт

Ты — DevOps-инженер. В репозитории `D:\6 семестр\Методы программирования\lab13` готова
мультиагентная система поддержки пациентов: Go-модуль `agents/` (универсальный агент
`cmd/agent`, автомасштабировщик `cmd/autoscaler`), Python-пакет `mas` (оркестратор,
LLM-агент, дашборд), роли `roles/*.md`, конфиги автомасштабировщика `deploy/autoscaler.*.yaml`.
Контракт — `docs/ARCHITECTURE.md`. Прочитай контракт, точки входа и переменные окружения
в коде (`agents/internal/config`, `mas/common/settings.py`, `mas/dashboard/settings.py`).
Локально Docker нет — всё, что связано с Docker, проверяется только в GitHub Actions,
поэтому пиши аккуратно и без «угадывания»: каждую переменную и путь сверяй с кодом.

### Что сделать

1. **Образы**:
   - `deploy/agent.Dockerfile` — многоэтапная сборка Go (`golang:1.27` → итоговый образ
     с обоими бинарниками `/app/agent`, `/app/autoscaler`, ролями в `/app/roles`, `tzdata` и
     CA-сертификатами; не root). Возьми за образец Dockerfile Go-сервиса из
     `D:\6 семестр\Методы программирования\lab11` (найди его сам), укажи источник в комментарии.
   - `deploy/python.Dockerfile` — `python:3.12-slim`, установка пакета, не root, роли
     в `/app/roles`; один образ для оркестратора/дашборда и LLM-агента (команда задаётся
     в compose).
   - `.dockerignore`.
2. **`compose.yaml`** в корне: `nats` (JetStream, `-js`, мониторинг 8222, healthcheck),
   `redis` (`redis:7-alpine`, volume для данных — состояние переживает пересоздание
   контейнера агента), `jaeger` (`jaegertracing/jaeger:2.22.0`, OTLP HTTP 4318, UI 16686),
   агенты `triage`, `booking-a`, `booking-b`, `reminder`, `feedback`, `llm-intake`,
   `llm-feedback-reply` (по умолчанию `LLM_MODE=fallback`; профиль `llm` добавляет сервис
   `ollama` и переключает агента на модель), `dashboard` (порт 8000, `DASHBOARD_HOST=0.0.0.0`,
   `AUTOSCALER_URL`, `JAEGER_UI_URL`), `autoscaler` (бэкенд `docker`, сокет
   `/var/run/docker.sock`, конфиг `deploy/autoscaler.docker.yaml` с реальными именами образа и
   сети compose — исправь заглушки в этом файле). Явное `name:` проекта, чтобы имя сети было
   предсказуемым. Зависимости через `depends_on: condition: service_healthy`. Обязательные
   переменные — через `${VAR:?}` там, где без них нельзя, остальные с умолчаниями. Пиновка
   образов по тегу (digest не нужен).
3. **Сквозная проверка** `scripts/e2e.py` (stdlib + `httpx`, без pytest), запускается на
   работающем стеке (`--base-url`, `--jaeger-url`, `--redis` и т. п.); печатает понятный
   отчёт и завершается кодом ≠ 0 при провале. Проверяет:
   - отправку 5 обращений через `POST /api/cases` дашборда и их завершение
     (`completed`, одно с красным флагом — `stopped`);
   - трассу запуска в Jaeger (`/api/v3/traces/{trace_id}` или поиск): в одной трассе спаны
     не менее чем 5 разных сервисов (orchestrator, llm-agent, agent-triage, agent-booking,
     agent-reminder, agent-feedback);
   - состояние в Redis: счётчики `mas:stats:reminder` после `docker compose restart reminder`
     не обнуляются (сам перезапуск делает CI-шаг, скрипт принимает режим `--phase before|after`
     и файл состояния);
   - автомасштабирование: после нагрузки (`POST /api/load` или пачка обращений при
     `AGENT_SLOW_MS` у триажа) автомасштабировщик сообщает в `/status` больше одного
     экземпляра триажа и появляется событие `scaled_up`.
4. **CI** `.github/workflows/ci.yml`:
   - job `go`: `go vet`, `go test -race -cover ./...` в `agents/`, `gofmt -l` пустой;
   - job `python`: ruff, pytest с покрытием; для интеграционных тестов нужен NATS с
     JetStream — сервисный контейнер GitHub не принимает аргументы команды, поэтому подними
     `nats:2.11 -js` шагом `docker run` и передай `NATS_URL`;
   - job `compose` (после двух первых): `docker compose config`, `up -d --build --wait`,
     `scripts/e2e.py`, перезапуск `reminder` и вторая фаза проверки, при провале —
     вывод `docker compose logs`; `down -v` в конце всегда;
   - кэш Go-модулей и pip, `permissions: contents: read`, `concurrency` с отменой
     устаревших запусков; документацию (`*.md`, `reports/**`) не гонять через CI
     (`paths-ignore`).
5. **Локальный запуск без Docker** `scripts/dev.ps1` (PowerShell 5.1): `start` / `stop` /
   `status` — поднимает нативные бинарники из `D:\dev\lab13-tools` (пути параметрами с
   умолчаниями: `nats-server.exe -js`, `jaeger.exe`, Garnet
   `win-x64-based-readytorun\net8.0\GarnetServer.exe --port 6379`), собирает Go-бинарники,
   запускает агентов, LLM-агентов (`.venv`), дашборд и автомасштабировщик (exec-бэкенд) в
   фоне с логами в `.run/logs/`, PID в `.run/pids.json`; `stop` останавливает только свои PID.
   Переменные окружения как в compose (`REMINDER_DEMO_DELAY` и т. п.).

### Ограничения

- Тесты приложения не пиши и код приложения не меняй, кроме `deploy/autoscaler.docker.yaml`.
  Если без правки кода никак — опиши в ответе, что и почему, но не правь.
- Проверь локально всё, что можно без Docker: `py -3.12 -m py_compile scripts/e2e.py`,
  `.venv/Scripts/ruff check scripts`, синтаксис YAML (`.venv/Scripts/python -c "import yaml..."`),
  `scripts/dev.ps1` — разбор PowerShell (`[System.Management.Automation.Language.Parser]::ParseFile`).
- Не делай git-коммитов. Комментарии — на русском, к неочевидному.

### Ответ

Верни коротко: файлы, переменные окружения по сервисам, допущения, что проверить в CI.

## Почему так

- Docker локально нет (решение из ЛР №11): compose и e2e проверяются в GitHub Actions,
  локально — нативные бинарники через `dev.ps1`.
- Сквозная проверка в CI — доказательство заданий 3–5 (трасса, Redis, масштабирование), а не
  только «контейнеры поднялись».
- LLM в CI по умолчанию в режиме fallback: Ollama с моделью в Actions — долго и нестабильно.

## Ответ генератора (итоговое сообщение, сокращено)

Проверка генератором: `py_compile` и ruff для `scripts`, YAML разобран (`compose.yaml`,
`deploy/autoscaler.docker.yaml`, `ci.yml`), `dev.ps1` — 0 ошибок разбора, `dev.ps1 status`
(только чтение). Docker-часть не запускалась — первый запуск в GitHub Actions.

Файлы: `deploy/agent.Dockerfile` (golang:1.27-alpine → alpine:3.21, пользователь 10001, образец —
`lab11/ingest/Dockerfile`), `deploy/python.Dockerfile`, `.dockerignore`, `compose.yaml` (`name: mas`,
сеть `mas_default`), `deploy/autoscaler.docker.yaml` (реальные имена образа и сети), `scripts/e2e.py`,
`.github/workflows/ci.yml`, `scripts/dev.ps1` (UTF-8 с BOM для PowerShell 5.1).

Допущения генератора:
- Профиль `llm` добавляет `ollama` и `ollama-pull`; режим модели — `LLM_MODE=llm`.
- `${VAR:?}` не использован: обязательных переменных нет.
- Образы собираются у `triage` (`mas-agent`) и `dashboard` (`mas-python`), остальные — `pull_policy: never`.
- У Jaeger нет healthcheck (в образе нет shell) — `e2e.py` сам ждёт трассу до 60 с.
- Масштабирование в CI: `TRIAGE_SLOW_MS=800`, 40 обращений через `POST /api/load`.
- e2e: фаза `before` (5 обращений, трасса ≥5 сервисов, нагрузка, снимок `mas:stats:reminder`), фаза `after` (счётчики не уменьшились, новая цепочка проходит).
- Автомасштабировщик в compose от root (gid docker.sock на хостах разный); в конце CI удаляются контейнеры с меткой `mas.autoscaler=1`.

Проверить в CI: Jaeger 2.22 на 0.0.0.0:4318/16686; `required: false` и `pull_policy: never` в Compose раннера; достижение порога масштабирования; сохранность счётчиков Redis после рестарта.

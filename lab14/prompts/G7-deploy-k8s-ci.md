# G7 — Docker, Compose, Kubernetes с HPA и CI (задание 5 и инфраструктура)

Модель-генератор: Claude Sonnet 5.5, effort medium (подагент, не видит ход работы).

## Промпт

Ты — DevOps-инженер. Репозиторий `D:\6 семестр\Методы программирования\lab14`: Go-модуль
`go/` (`cmd/sensorsim`, `cmd/collector`), Python-пакет `vitals` (`python/`,
`pyproject.toml`), Rust-крейт `rust/vitals_validate` (PyO3/maturin). Контракт —
`docs/ARCHITECTURE.md` (разделы 4, 5, 11, 13). Переменные окружения сверяй с кодом
(`go/internal/config`, `python/vitals/common`) — **ничего не угадывай**. Docker локально
нет: всё, что связано с Docker и Kubernetes, впервые запустится в GitHub Actions.

### Что сделать

1. **Образы**: `deploy/go.Dockerfile` (многоэтапный, оба бинарника, не root, CA и tzdata);
   `deploy/python.Dockerfile` (многоэтапный: сборка Rust-колеса maturin на первом этапе,
   установка пакета и колеса во второй; не root); `.dockerignore`.
2. **`compose.yaml`**: etcd (`quay.io/coreos/etcd:v3.6.x` или новее — проверь тег),
   NATS (`-js`), sensorsim, два сборщика `COORD=etcd` с `SINKS=files,nats,flight`,
   streaming-потребитель, дашборд (порт 8000). healthcheck и `depends_on: service_healthy`.
3. **Kubernetes `k8s/`**: Namespace, etcd (StatefulSet 1 реплика или Deployment +
   Service), NATS, sensorsim, collector (Deployment, `resources.requests.cpu` обязателен
   для HPA, readiness по `/status`), HPA `autoscaling/v2` (CPU 60%, 1–4 реплики,
   `behavior` с быстрым ростом и медленным уменьшением), Service-ы; `kustomization.yaml`.
4. **CI `.github/workflows/ci.yml`**:
   - `go`: vet, `test -race -cover`, gofmt;
   - `rust`: `cargo test`, `cargo clippy -D warnings` для `rust/vitals_validate`;
   - `python`: собрать колесо maturin, установить пакет, ruff, pytest с покрытием; для
     интеграционных тестов поднять `nats -js` и `etcd` шагами `docker run` (сервисные
     контейнеры не принимают аргументы), передать `NATS_URL`/`ETCD_ENDPOINTS`;
   - `compose` (после трёх первых): `up -d --build --wait`, сквозная проверка
     `scripts/e2e.py` (напиши: через 60 с — raw/agg-файлы у обоих сборщиков, устройства
     поделены без пересечений по `/status`, в NATS есть сообщения, `/api/snapshot`
     дашборда не пуст, Arrow `DoGet` отдаёт строки; остановка одного сборщика → второй
     забирает его устройства), логи при провале, `down -v`;
   - `k8s`: kind (`helm/kind-action` или установка kind вручную), metrics-server с
     `--kubelet-insecure-tls`, `kind load docker-image`, `kubectl apply -k k8s/`, ожидание
     готовности, затем увеличение нагрузки (патч env эмулятора: больше `DEVICES`, меньше
     `PERIOD_MS`) и ожидание, что HPA поднял реплики сборщика > 1 (таймаут ~6 мин, вывод
     `kubectl get hpa,pods` и `top pods` в лог); сохранить вывод как артефакт;
   - кэш Go/pip/cargo, `permissions: contents: read`, `concurrency`, `paths-ignore` для
     `**.md`, `reports/**`, `prompts/**`.
5. **`scripts/dev.ps1`** (PowerShell 5.1, UTF-8 с BOM): `start|stop|status` локального стенда
   без Docker — etcd `D:\dev\lab14-tools\etcd\etcd-v3.7.2-windows-amd64\etcd.exe`, NATS
   `D:\dev\lab13-tools\bin\nats-server.exe -js`, эмулятор, два сборщика, потребитель,
   дашборд; логи `.run/logs`, PID `.run/pids.json`, `stop` — только свои PID.

### Ограничения

- Код приложения не меняй; если без правки нельзя — опиши в ответе.
- Проверь локально что можно: YAML (`.venv\Scripts\python -c "import yaml"`), `py_compile` и
  ruff для `scripts/`, разбор `dev.ps1` парсером PowerShell, `kubectl`-манифесты — только
  синтаксис YAML. Не запускай `dev.ps1 start`.
- Не делай git-коммитов. Файлы — инструментом Write. Комментарии на русском, к неочевидному.

### Ответ

Коротко: файлы, что проверить при первом запуске CI, допущения.

## Почему так

- Kubernetes проверяется в CI через kind — на машине разработки нет Docker (как в ЛР №11, 13).
- HPA по CPU, а горизонтальное масштабирование сборщиков работает «само» благодаря HRW в
  etcd — это и есть смысл задания 5 вместе с заданием 1.
- e2e проверяет поведение (разбиение, перехват устройств, данные во всех стоках), а не
  только «контейнеры поднялись» — урок ЛР №13 (там e2e засчитывал провалы как успех).

## Ответ генератора (итоговое сообщение, сокращено)

Проверка генератором: YAML разбирается, ruff и `py_compile` для `scripts/` чисто, `dev.ps1` разобран
парсером PowerShell, теги образов существуют. Docker, Kubernetes и CI — первый запуск в GitHub Actions.

Файлы: `.dockerignore`, `deploy/go.Dockerfile` (golang:1.27-alpine → alpine:3.22, uid 10001),
`deploy/python.Dockerfile` (колесо maturin на первом этапе), `compose.yaml` (etcd v3.7.2, nats -js,
sensorsim, collector-1/2, streaming, dashboard :8000), `k8s/` (namespace, etcd, nats, sensorsim,
collector, hpa, kustomization), `.github/workflows/ci.yml` (go, rust, python, compose, k8s),
`scripts/e2e.py`, `scripts/dev.ps1`.

Допущения генератора: `FLIGHT_ADVERTISE=collector-N:8815` в compose; в k8s сборщик `SINKS=files,nats`,
`EMIT=agg` без Flight (не раздувать RSS), `COLLECTOR_ID` = имя пода, initContainer ждёт зависимости;
etcd и NATS в k8s — Deployment; нагрузка для HPA — `DEVICES=3000`, `PERIOD_MS=50` и `rollout restart`
сборщиков (список устройств кэшируется при старте); `cargo test` с `LD_LIBRARY_PATH` из `sysconfig`;
`dev.ps1 stop` — `taskkill /PID /T /F` только по своим PID.

Проверить в CI: сборку колеса в образе (toolchain копируется из `rust:1.99-slim`), healthcheck etcd через
`etcdctl`, `cargo test` с libpython, достижение 60% CPU для HPA.

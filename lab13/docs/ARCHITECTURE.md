# Архитектура: мультиагентная система поддержки пациентов

Документ — контракт между компонентами. Все форматы сообщений, субъекты NATS и правила
ниже обязательны для агентов на Go, оркестратора, LLM-агента и дашборда на Python.

## 1. Компоненты

| Компонент | Язык | Каталог | Назначение |
|---|---|---|---|
| Универсальный агент | Go | `agents/cmd/agent` | Один бинарник, поведение задаёт файл роли `roles/<role>.md` (`-role`). Виды (`kind`): `triage`, `booking`, `reminder`, `feedback` |
| Автомасштабировщик | Go | `agents/cmd/autoscaler` | Следит за длиной очередей JetStream и запускает/останавливает доп. экземпляры агентов |
| Оркестратор | Python (asyncio, nats-py) | `mas/orchestrator` | Управляет цепочкой (pipeline), аукционом, повторами и таймаутами |
| LLM-агент | Python | `mas/llm_agent` | Классификация обращения и генерация ответа на отзыв через Ollama; без модели — правила (fallback) |
| Дашборд | Python (FastAPI + Jinja2) | `mas/dashboard` | Статус агентов, очереди, запуски, журнал событий, трассы, ручной запуск. В одном процессе с оркестратором |
| Инфраструктура | — | `deploy/` | NATS (JetStream), Redis, Jaeger, Ollama (по желанию) |

Агенты предметной области (вариант 18): **Триаж**, **Запись к врачу**, **Напоминание о
приёме**, **Сбор обратной связи** + LLM-агент **Анализ обращения** (задание 7).

## 2. Цепочка обработки обращения (pipeline)

```
intake (LLM, Python) → triage (Go) → booking (аукцион Go-агентов) → reminder (Go, Redis) → feedback (Go)
```

1. **intake** — свободный текст жалобы → структурированные симптомы, предполагаемая
   специальность, «красные флаги».
2. **triage** — правила срочности: `emergency` | `urgent` | `routine`, максимальное
   ожидание приёма. При `emergency` оркестратор **останавливает цепочку** со статусом
   `stopped` и рекомендацией «Вызовите скорую: 103» — запись не создаётся.
3. **booking** — оркестратор проводит аукцион среди агентов записи (раздел 6), победитель
   бронирует слот.
4. **reminder** — планирует напоминания о приёме, хранит их в Redis.
5. **feedback** — планирует опрос после приёма.

Результат шага *n* целиком (`output`) передаётся во вход шага *n+1* в поле
`payload.previous`, плюс накопленный контекст `payload.case` (данные обращения).

Отдельная короткая цепочка для отзыва пациента:
`feedback (kind=analyze) → [если отзыв негативный] feedback_reply (LLM)`.

## 3. Субъекты NATS

| Субъект | Тип | Кто публикует → кто читает |
|---|---|---|
| `tasks.<step>` | JetStream, поток `TASKS` | оркестратор → агенты роли `<step>` (`intake`, `triage`, `reminder`, `feedback`, `feedback_reply`) |
| `results.<run_id>` | core NATS | агент → оркестратор (подписка `results.>`) |
| `auction.booking` | core NATS, request с несколькими ответами | оркестратор → все агенты записи; ответы — ставки на inbox запроса |
| `agent.<agent_id>.book` | core NATS request-reply | оркестратор → победитель аукциона |
| `agents.heartbeat` | core NATS | каждый агент раз в 5 с → дашборд, автомасштабировщик |
| `events.<source>` | core NATS | все компоненты → дашборд (журнал «кто что получил/передал») |
| `notify.patient` | core NATS | напоминания/опросы «отправлены пациенту» (имитация SMS) → дашборд |

Поток JetStream `TASKS`: subjects `tasks.>`, retention **WorkQueue**, storage file,
max_age 24h. Для каждого шага — durable pull-консьюмер `step-<step>` с фильтром
`tasks.<step>`, `AckWait` 30 с (для `intake`/`feedback_reply` 120 с), `MaxDeliver` 5,
ack explicit. Все экземпляры одного шага читают **один** консьюмер — так JetStream
распределяет задачи между экземплярами (балансировка), а `num_pending` консьюмера —
длина очереди для автомасштабирования. Поток и консьюмеры создаются идемпотентно
(«создать или обновить») и оркестратором, и агентом при старте.

## 4. Форматы сообщений (JSON, UTF-8)

Время — строки RFC 3339 в UTC (`2026-10-13T09:30:00Z`). Идентификаторы — UUID v4 строкой.

### 4.1 Задача (`tasks.<step>`)

```json
{
  "task_id": "uuid",
  "run_id": "uuid",
  "step": "triage",
  "kind": "triage",
  "attempt": 1,
  "created_at": "2026-10-13T09:30:00Z",
  "payload": { "case": { ... }, "previous": { ... } }
}
```

Заголовки NATS: `traceparent` (W3C Trace Context, обязательно), `Nats-Msg-Id` =
`<task_id>-<attempt>` (дедупликация JetStream).

### 4.2 Результат (`results.<run_id>`)

```json
{
  "task_id": "uuid",
  "run_id": "uuid",
  "step": "triage",
  "agent_id": "triage-1",
  "success": true,
  "output": { ... },
  "error": "",
  "duration_ms": 12,
  "finished_at": "2026-10-13T09:30:00Z"
}
```

`success=false` + `error` — бизнес-ошибка или сбой обработки (оркестратор решает,
повторять ли). Агент **ack-ает** задачу после публикации результата в обоих случаях;
не ack-ает (nak) только если не смог опубликовать результат.

### 4.3 Обращение (`payload.case`)

```json
{
  "patient_id": "p-001",
  "patient_name": "Иванова Анна Петровна",
  "age": 34,
  "phone": "+7 900 000-00-01",
  "complaint": "Третий день температура 38, кашель, болит горло",
  "preferred_specialty": ""
}
```

Ограничения: `complaint` 3..2000 символов, `age` 0..120, `patient_name` 1..200.

### 4.4 Выходы шагов (`output`)

**intake** (LLM-агент):
```json
{"symptoms": ["температура", "кашель"], "specialty": "therapist",
 "red_flags": [], "summary": "ОРВИ-подобные симптомы 3 дня", "source": "llm"}
```
`specialty` — только из справочника (раздел 7); `source` = `llm` | `fallback`.

**triage**:
```json
{"urgency": "routine", "specialty": "therapist", "max_wait_hours": 72,
 "reasons": ["температура без красных флагов"], "recommendation": "Запись к терапевту"}
```
`emergency` → `max_wait_hours: 0`, `specialty` может быть пустым.

**booking** (ответ победителя на `agent.<id>.book`, оркестратор кладёт его в `output`):
```json
{"appointment_id": "uuid", "doctor_id": "d-therapist-1", "doctor_name": "...",
 "specialty": "therapist", "starts_at": "...", "ends_at": "...", "agent_id": "booking-1",
 "auction": {"bids": 3, "winner_cost": 0.41}}
```

**reminder**:
```json
{"reminders": [{"reminder_id": "uuid", "send_at": "...", "channel": "sms", "status": "scheduled"}]}
```

**feedback** (`kind` задачи `schedule_survey`):
```json
{"survey_id": "uuid", "send_at": "...", "channel": "sms"}
```

**feedback** (`kind` задачи `analyze`, вход `{"appointment_id", "patient_id", "rating": 1..5, "text"}`):
```json
{"rating": 2, "sentiment": "negative", "escalate": true, "topics": ["ожидание"],
 "nps_bucket": "detractor"}
```

**feedback_reply** (LLM): `{"reply": "текст ответа пациенту", "source": "llm"}`.

## 5. Оркестратор: повторы и таймауты

- Таймаут ожидания результата шага: по умолчанию 15 с, для LLM-шагов 90 с (настраивается).
- До **3 попыток** на шаг (`attempt` 1..3). Повтор — при таймауте или `success=false`
  с ошибкой, помеченной как временная (`"retryable": true` в результате). После
  3 неудач запуск получает статус `failed`, причина пишется в журнал.
- Результат с неизвестным `task_id` или от устаревшей попытки игнорируется.
- Статусы запуска (run): `running` → `completed` | `stopped` (emergency или нет свободных
  врачей — с рекомендацией) | `failed` (сбой системы).
- Оркестратор не хранит соединение при импорте: подключение создаётся в lifespan
  приложения/фабрике.

## 6. Аукцион записи

1. Оркестратор шлёт `auction.booking` (request, ответы собираются **500 мс**):
   ```json
   {"auction_id": "uuid", "run_id": "uuid", "specialty": "therapist",
    "urgency": "routine", "max_wait_hours": 72, "now": "...", "exclude_agents": []}
   ```
2. Каждый агент записи отвечает ставкой по **лучшему** своему врачу:
   ```json
   {"auction_id": "uuid", "agent_id": "booking-1", "doctor_id": "d-therapist-1",
    "slot_start": "...", "skill": 1.0, "availability": 0.83, "load": 0.25, "cost": 0.41,
    "eligible": true, "reason": ""}
   ```
   - `skill`: 1.0 — специальность врача совпадает; 0.5 — врач `therapist`, а нужна другая
     специальность и срочность не `urgent`; иначе 0 и `eligible=false`.
   - `wait_h` — часы от `now` до ближайшего свободного слота; если `wait_h > max_wait_hours`
     → `eligible=false`.
   - `availability = 1 - wait_h / max_wait_hours` (0..1).
   - `load` — доля занятых слотов врача на ближайшие 7 дней (0..1).
   - **`cost = 0.5·(1 - availability) + 0.3·load + 0.2·(1 - skill)`**; меньше — лучше.
3. Победитель — минимальный `cost` среди `eligible`; при равенстве — меньший `slot_start`,
   затем `agent_id` по алфавиту. Ставки со `slot_start` не по RFC 3339 (без часового пояса)
   пропускаются.
   **Нет подходящих ставок (`no_bids`)** — это исход для пациента, а не сбой системы
   (уточнено после находки №10): плановое обращение (`routine`) получает второй аукцион
   с окном, расширенным в `no_bids_extend_factor` раз (по умолчанию 4: 72 ч → 12 суток);
   если ставок снова нет или обращение срочное (`urgent`), запуск получает статус
   `stopped` с рекомендацией (неотложная помощь / регистратура свяжется с пациентом).
4. Оркестратор шлёт победителю `agent.<id>.book`:
   `{"auction_id", "run_id", "doctor_id", "slot_start", "patient_id", "patient_name", "max_wait_hours"}`.
   Агент бронирует атомарно (Redis `SET slot:<doctor_id>:<slot_start> <appointment_id> NX`).
   Слот уже занят (параллельный аукцион успел раньше) → агент записывает на ближайший
   свободный слот **того же врача** не позже `now + max_wait_hours` (уточнено после находки
   №12: при одновременных обращениях все аукционы видят один ближайший слот). Нет такого
   слота → `{"success": false, "error": "slot_taken"}`, оркестратор проводит аукцион
   заново (до 3 раундов).

## 7. Справочник специальностей

`therapist` (терапевт), `cardiologist`, `neurologist`, `ophthalmologist`,
`dermatologist`, `ent` (ЛОР), `traumatologist`. Врачи и их графики (рабочие дни, смена,
шаг слота) задаются в файлах ролей агентов записи (`roles/booking-*.md`). Расписание
врача — та же модель, что в ЛР №12 (`app/services/scheduling.py`): полуоткрытые
интервалы, сетка от начала смены, местное время клиники `Europe/Moscow`.

## 8. Файл роли (`roles/*.md`)

Рекомендация преподавателя: универсальный агент + роль в Markdown. Формат:

```markdown
# Роль: Триаж
## Роль
...кто агент и за что отвечает...
## Правила
1. ...
## Стиль общения
...
## Специализация
...
## Параметры
```yaml
kind: triage
step: triage
concurrency: 4
...параметры вида агента...
```
```

Агент Go читает **первый** блок ```` ```yaml ```` после заголовка `## Параметры`
(обязательные поля `kind`, `step`), текст разделов — для heartbeat и дашборда. LLM-агент
собирает системный промпт из разделов «Роль», «Правила», «Стиль общения», «Специализация».
Неизвестный `kind` или битый YAML — агент не стартует (код выхода 2, понятная ошибка).

## 9. Heartbeat (`agents.heartbeat`, раз в 5 с)

```json
{"agent_id": "triage-1", "role": "triage", "kind": "triage", "title": "Триаж",
 "status": "ready", "processed": 10, "failed": 1, "inflight": 0,
 "started_at": "...", "sent_at": "...", "host": "pid-1234", "lang": "go"}
```
Агент считается «в сети», если heartbeat моложе 15 с. При остановке шлёт `status: "stopping"`.

## 10. Журнал событий и трассировка

**Событие** (`events.<source>`):
```json
{"ts": "...", "source": "triage-1", "action": "received", "run_id": "uuid",
 "task_id": "uuid", "step": "triage", "trace_id": "hex32", "span_id": "hex16",
 "subject": "tasks.triage", "summary": "urgency=routine"}
```
`action`: `received`, `completed`, `failed`, `published`, `bid`, `won`, `booked`,
`retry`, `timeout`, `stopped`, `scaled_up`, `scaled_down`, `notified`.

**OpenTelemetry**: сервисы `orchestrator`, `agent-<role>`, `llm-agent`, `autoscaler`.
Экспорт OTLP/HTTP в Jaeger (`OTEL_EXPORTER_OTLP_ENDPOINT`, по умолчанию
`http://localhost:4318`). Контекст передаётся в заголовке NATS `traceparent`.
Один запуск (run) = одна трасса: корневой спан `pipeline.run` в оркестраторе, дочерние —
`step <name>` (оркестратор), `process <step>` (агент), `auction.bid`, `booking.book`,
`redis ...`. Без Jaeger система работает: экспортёр не должен блокировать обработку.

## 11. Состояние в Redis (задание 4)

Ключи (префикс `mas:`):

| Ключ | Тип | Чей | Что |
|---|---|---|---|
| `mas:slot:<doctor_id>:<slot_start>` | string | booking | id записи, `SET NX` — защита от двойной записи между экземплярами |
| `mas:appt:<appointment_id>` | hash | booking | данные записи |
| `mas:doctor:<doctor_id>:booked` | zset | booking | занятые слоты врача: member = `slot_start`, score = unix-время начала (для поиска свободных слотов и `load`) |
| `mas:surveys:due` / `mas:survey:<id>` | zset / hash | feedback | запланированные опросы, тот же механизм, что у напоминаний |
| `mas:reminders:due` | zset | reminder | reminder_id, score = unix-время отправки |
| `mas:reminder:<id>` | hash | reminder | данные напоминания, статус |
| `mas:stats:<agent_role>` | hash | все Go-агенты | счётчики `processed`, `failed`, `sent` — восстанавливаются после рестарта |
| `mas:feedback:stats` | hash | feedback | количество отзывов по оценкам 1..5, NPS |

Агент напоминаний после рестарта читает `mas:reminders:due` и продолжает отправлять
просроченные и будущие напоминания; счётчики продолжаются с сохранённых значений.
Отправку «забирает» тот экземпляр, чей `ZREM` вернул 1, — так несколько экземпляров
не отправят одно напоминание дважды.

## 12. Динамическое масштабирование (задание 5)

Автомасштабировщик раз в `interval` (2 с) читает `num_pending + num_ack_pending`
консьюмера каждого управляемого шага. Желаемое число экземпляров
`desired = clamp(ceil(backlog / per_instance), min, max)`; увеличение — сразу,
уменьшение — только после `cooldown` (30 с) без роста очереди. Бэкенды:
`exec` (локальные процессы агента) и `docker` (Docker Engine API через
`/var/run/docker.sock`: создать и запустить контейнер из образа агента в сети compose,
остановить и удалить при уменьшении). Каждое действие — событие `scaled_up`/`scaled_down`.

## 13. Безопасность и надёжность

- Текст пациента в LLM-промпте — внутри явных разделителей, с указанием считать его
  данными; ответ модели валидируется по схеме, специальность — только из справочника;
  при невалидном ответе — fallback на правила.
- Красные флаги определяются **правилами в Go-агенте триажа** независимо от LLM:
  модель может только добавить флаг, но не убрать.
- Дашборд: CSP без `unsafe-inline`, данные выводятся через `textContent`/экранирование
  Jinja2, без `innerHTML`.
- Секреты и адреса — только из переменных окружения; в compose — `${VAR:?}` для
  обязательных.
- Graceful shutdown: агент прекращает выборку задач, дорабатывает текущие (до 10 с),
  отправляет heartbeat `stopping`, закрывает соединения.

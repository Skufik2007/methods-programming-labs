# Задание 3. Распределённая трассировка (Jaeger)

**Требование:** OpenTelemetry в агентах и оркестраторе, сбор трасс в Jaeger, визуализация
полного пути распределённой задачи. Преподаватель уточнил, что минимум — журнал «какой
агент что получил и что передал».

## Что сделано

Сделаны оба варианта: трассы OpenTelemetry в Jaeger и журнал событий на дашборде.

**OpenTelemetry → Jaeger.**
- Экспорт идёт по OTLP/HTTP (`OTEL_EXPORTER_OTLP_ENDPOINT`) пакетами. Недоступный Jaeger
  обработку не блокирует.
- Сервисы в трассах: `orchestrator`, `llm-agent`, `agent-triage`, `agent-booking`,
  `agent-reminder`, `agent-feedback`, `autoscaler`.
- Один запуск — одна трасса. Корневой спан — `pipeline.run`, под ним спаны `step <шаг>`
  оркестратора. В них вложены спаны агентов `process <шаг>`, `auction.bid` и `booking.book`,
  а также спаны операций Redis.
- Контекст передаётся в заголовке NATS `traceparent` (W3C Trace Context): в Go —
  `propagation.TraceContext` поверх `nats.Header`, в Python — `TraceContextTextMapPropagator`.
- Отложенные действия тоже попадают в трассу запуска. Контекст сохраняется в Redis вместе
  с напоминанием, поэтому SMS-напоминание, отправленное через 20 с, и опрос через 40 с
  видны как спаны `dispatch ...`.

**Журнал событий.**
- Каждый компонент публикует события `events.<источник>` (`received`, `completed`,
  `published`, `bid`, `won`, `booked`, `retry`, `timeout`, `notified`, `scaled_up` и др.)
  с `trace_id`.
- Дашборд хранит последние 2000 событий и показывает журнал запуска по времени. Со страницы
  запуска есть ссылка «Открыть в Jaeger».

## Проверка

- Jaeger (локально, v2.22): трасса запуска из формы дашборда — 6 сервисов, 36 спанов,
  глубина 4. Скриншот — [img/jaeger-trace.jpg](img/jaeger-trace.jpg).
- Журнал запуска на дашборде — [img/dashboard-run-journal.jpg](img/dashboard-run-journal.jpg).
- CI (e2e, Jaeger в Docker): «трасса …: не менее 5 сервисов в одной трассе — сервисы:
  agent-booking, agent-feedback, agent-reminder, agent-triage, llm-agent, orchestrator».
- Тест [`tests/integration/test_bus_trace.py`](../tests/integration/test_bus_trace.py):
  запросы аукциона и брони несут `traceparent` текущей трассы (запуск на настоящем NATS).

## Находки при проверке

| № | Что было | Исправление |
|---|---|---|
| 1 | В Jaeger трассы запусков содержали 5 сервисов: спаны `agent-booking` попадали в отдельные трассы, потому что запросы аукциона и брони уходили без `traceparent` | Заголовок добавлен в `request()` и `collect()` шины. Тест падал до правки |

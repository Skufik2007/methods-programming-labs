# Задание 1. Полная система из 3–5 агентов на Go

**Требование:** реализовать всех агентов своей предметной области, каждый — отдельный
микросервис на Go, общение через NATS; агенты разных типов.

## Что сделано

Вариант 18 «Система поддержки пациентов» требует четырёх агентов. Все они — экземпляры
**одного универсального Go-агента** ([`agents/cmd/agent`](../agents/cmd/agent)). Поведение
задаёт файл роли в Markdown: так рекомендовал преподаватель («универсальный агент + роль
в Markdown: роль, правила, стиль общения, специализация»).

| Агент | Файл роли | Тип | Как получает работу |
|---|---|---|---|
| Триаж | [`roles/triage.md`](../roles/triage.md) | анализатор/валидатор: срочность, специальность, красные флаги | JetStream `tasks.triage` |
| Запись к врачу ×2 | [`roles/booking-a.md`](../roles/booking-a.md), [`roles/booking-b.md`](../roles/booking-b.md) | исполнитель: аукцион и бронь, у каждого свои врачи | core NATS `auction.booking`, `agent.<id>.book` |
| Напоминание о приёме | [`roles/reminder.md`](../roles/reminder.md) | исполнитель с состоянием в Redis | JetStream `tasks.reminder` |
| Сбор обратной связи | [`roles/feedback.md`](../roles/feedback.md) | исполнитель + анализатор отзывов | JetStream `tasks.feedback` |

Каждый запуск `agent -role roles/<роль>.md` — отдельный процесс (в compose — отдельный
контейнер). Файл роли разбирается пакетом [`internal/role`](../agents/internal/role):
разделы Markdown идут в heartbeat и на дашборд, YAML-блок `## Параметры` — в параметры
вида агента. Неизвестный `kind` или битый YAML — агент не стартует (код выхода 2).

Общий каркас: шина и JetStream ([`internal/bus`](../agents/internal/bus)), телеметрия,
логи slog (stdout + файл), heartbeat раз в 5 с, события журнала, graceful shutdown.
Бизнес-логика каждого вида — отдельный пакет без NATS и Redis ([`triage`](../agents/internal/triage),
[`booking`](../agents/internal/booking), [`reminder`](../agents/internal/reminder),
[`feedback`](../agents/internal/feedback)), поэтому тестируется без инфраструктуры.

Расписание врачей перенесено на Go из ЛР №12 (`app/services/scheduling.py` → пакет
[`internal/schedule`](../agents/internal/schedule)): полуоткрытые интервалы, сетка от начала
смены, местное время `Europe/Moscow`.

## Проверка

- Локально (нативные nats-server, Garnet, Jaeger): 5 Go-агентов и 2 LLM-агента в сети,
  скриншот — [img/dashboard-overview.jpg](img/dashboard-overview.jpg).
- В CI (Docker Compose): шаг e2e «все агенты в сети — booking=2, feedback=1,
  feedback_reply=1, intake=1, reminder=1, triage=1».
- Тесты Go: `go test ./...` в [`agents/`](../agents), в том числе интеграционные со
  встроенным nats-server — см. [README](../README.md#тесты).

## Находки при проверке

| № | Что было | Исправление |
|---|---|---|
| 4 | Повторная доставка задачи (после таймаута) создавала напоминания с новыми UUID — пациент получал SMS дважды | Детерминированные id (UUID v5 по записи) + однократное планирование `SET NX` |
| 12 | 21 из 40 одновременных обращений падали с `slot_taken`: все аукционы выбирали один ближайший слот | Агент записи занимает ближайший свободный слот того же врача в окне пациента |

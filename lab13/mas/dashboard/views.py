"""Представление данных: подписи на русском, форматирование, строки для таблиц и JSON API.

Все поля для отображения (`status_label`, `ts_fmt`...) считаются здесь один раз, чтобы
серверный шаблон и автообновление через JS показывали одно и то же.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from mas.common.models import Event, parse_ts
from mas.orchestrator.store import RunRecord, StepRecord

STEP_LABELS = {
    "intake": "Анализ обращения",
    "triage": "Триаж",
    "booking": "Запись к врачу",
    "reminder": "Напоминание о приёме",
    "feedback": "Обратная связь",
    "feedback_reply": "Ответ на отзыв",
}

SPECIALTY_LABELS = {
    "therapist": "Терапевт",
    "cardiologist": "Кардиолог",
    "neurologist": "Невролог",
    "ophthalmologist": "Офтальмолог",
    "dermatologist": "Дерматолог",
    "ent": "ЛОР",
    "traumatologist": "Травматолог",
}

ACTION_LABELS = {
    "received": "получил",
    "completed": "выполнил",
    "failed": "ошибка",
    "published": "передал в очередь",
    "bid": "ставка",
    "won": "выиграл аукцион",
    "booked": "записал",
    "retry": "повтор",
    "timeout": "таймаут",
    "stopped": "остановил цепочку",
    "scaled_up": "добавил экземпляр",
    "scaled_down": "убрал экземпляр",
    "notified": "уведомил пациента",
}

# (подпись, css-класс): цвет всегда дублируется текстом.
RUN_STATUS = {
    "running": ("выполняется", "warn"),
    "completed": ("завершён", "ok"),
    "stopped": ("остановлен: экстренный случай", "warn"),
    "failed": ("ошибка", "bad"),
}
STEP_STATUS = {
    "pending": ("ожидает", "muted"),
    "running": ("выполняется", "warn"),
    "completed": ("выполнен", "ok"),
    "failed": ("ошибка", "bad"),
    "skipped": ("пропущен", "muted"),
}
RUN_KIND = {"case": "обращение", "feedback": "отзыв"}

OUTPUT_KEYS = {
    "urgency": "срочность",
    "specialty": "специальность",
    "symptoms": "симптомы",
    "red_flags": "красные флаги",
    "summary": "кратко",
    "source": "источник",
    "max_wait_hours": "макс. ожидание, ч",
    "reasons": "причины",
    "recommendation": "рекомендация",
    "appointment_id": "id записи",
    "doctor_id": "id врача",
    "doctor_name": "врач",
    "starts_at": "начало",
    "ends_at": "конец",
    "agent_id": "агент",
    "auction": "аукцион",
    "bids": "ставок",
    "winner_cost": "стоимость победителя",
    "reminders": "напоминания",
    "reminder_id": "id напоминания",
    "send_at": "отправить в",
    "channel": "канал",
    "status": "статус",
    "survey_id": "id опроса",
    "rating": "оценка",
    "sentiment": "тональность",
    "escalate": "эскалация",
    "topics": "темы",
    "nps_bucket": "категория NPS",
    "reply": "ответ пациенту",
}
URGENCY_LABELS = {"emergency": "экстренно", "urgent": "срочно", "routine": "планово"}

MAX_OUTPUT_CHARS = 4000


def fmt_time(value: str) -> str:
    """`2026-10-13T09:30:00Z` → `13.10 09:30:00` (UTC). Нераспознанное — как есть."""
    if not value:
        return "—"
    try:
        return parse_ts(value).astimezone(UTC).strftime("%d.%m %H:%M:%S")
    except ValueError:
        return value


def fmt_duration(start: str, end: str) -> str:
    if not start or not end:
        return "—"
    try:
        seconds = (parse_ts(end) - parse_ts(start)).total_seconds()
    except ValueError:
        return "—"
    return f"{max(seconds, 0):.0f} с" if seconds >= 10 else f"{max(seconds, 0):.1f} с"


def short_id(value: str) -> str:
    return value[:8]


# ---- читаемый вывод результата шага -----------------------------------------------------


def _scalar(value: Any) -> str:
    if value is True:
        return "да"
    if value is False:
        return "нет"
    if value is None or value == "":
        return "—"
    return str(value)


def format_output(value: Any, indent: int = 0) -> str:
    """Вложенный словарь/список в строки «ключ: значение». Только текст: выводится как textContent/экранируется."""
    pad = "  " * indent
    lines: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            label = OUTPUT_KEYS.get(str(key), str(key))
            if isinstance(item, dict | list) and item:
                if isinstance(item, list) and all(not isinstance(x, dict | list) for x in item):
                    lines.append(f"{pad}{label}: {', '.join(_scalar(x) for x in item)}")
                else:
                    lines.append(f"{pad}{label}:")
                    lines.append(format_output(item, indent + 1))
            else:
                text = _scalar(item if not isinstance(item, dict | list) else "")
                if key == "urgency":
                    text = URGENCY_LABELS.get(text, text)
                elif key == "specialty":
                    text = SPECIALTY_LABELS.get(text, text)
                lines.append(f"{pad}{label}: {text}")
    elif isinstance(value, list):
        for index, item in enumerate(value, 1):
            if isinstance(item, dict | list):
                lines.append(f"{pad}#{index}")
                lines.append(format_output(item, indent + 1))
            else:
                lines.append(f"{pad}- {_scalar(item)}")
    else:
        lines.append(f"{pad}{_scalar(value)}")
    text = "\n".join(line for line in lines if line != "")
    return text if len(text) <= MAX_OUTPUT_CHARS else text[:MAX_OUTPUT_CHARS] + "\n…"


# ---- строки таблиц -----------------------------------------------------------------------


def agent_row(raw: dict[str, Any]) -> dict[str, Any]:
    """Строка агента из `AgentRegistry.snapshot()`."""
    if raw.get("online"):
        label, css = "в сети", "ok"
    elif raw.get("status") == "stopping" and raw.get("age_s", 0) < 15:
        label, css = "останавливается", "warn"
    else:
        label, css = "нет связи", "bad"
    age = raw.get("age_s")
    return {
        "agent_id": raw.get("agent_id", ""),
        "role": raw.get("role", ""),
        "title": raw.get("title", ""),
        "kind": raw.get("kind", ""),
        "lang": raw.get("lang", ""),
        "host": raw.get("host", ""),
        "status": raw.get("status", ""),
        "status_label": label,
        "status_class": css,
        "processed": raw.get("processed", 0),
        "failed": raw.get("failed", 0),
        "inflight": raw.get("inflight", 0),
        "age_s": age,
        "last_heartbeat": f"{fmt_time(raw.get('sent_at', ''))} ({age:.0f} с назад)" if age is not None else "—",
    }


def queue_row(raw: dict[str, Any]) -> dict[str, Any]:
    step = str(raw.get("step", ""))
    return {
        "step": step,
        "step_label": STEP_LABELS.get(step, step),
        "consumer": raw.get("consumer", f"step-{step}"),
        "pending": raw.get("pending", 0),
        "ack_pending": raw.get("ack_pending", 0),
        "redelivered": raw.get("redelivered", 0),
    }


def patient_of(run: RunRecord) -> str:
    case = run.case
    return str(case.get("patient_name") or case.get("patient_id") or "—")


def run_row(run: RunRecord) -> dict[str, Any]:
    label, css = RUN_STATUS.get(run.status, (run.status, "muted"))
    return {
        "run_id": run.run_id,
        "run_short": short_id(run.run_id),
        "kind": run.kind,
        "kind_label": RUN_KIND.get(run.kind, run.kind),
        "status": run.status,
        "status_label": label,
        "status_class": css,
        "patient": patient_of(run),
        "started_at": run.started_at,
        "started_fmt": fmt_time(run.started_at),
        "duration": fmt_duration(run.started_at, run.finished_at),
        "error": run.error,
        "recommendation": run.recommendation,
        "progress": _progress(run),
    }


def _progress(run: RunRecord) -> str:
    done = sum(1 for s in run.steps if s.status == "completed")
    return f"{done}/{len(run.steps)}" if run.steps else "—"


def step_row(step: StepRecord) -> dict[str, Any]:
    label, css = STEP_STATUS.get(step.status, (step.status, "muted"))
    return {
        "name": step.name,
        "step_label": STEP_LABELS.get(step.name, step.name),
        "kind": step.kind,
        "agent_id": step.agent_id or "—",
        "attempts": step.attempts,
        "duration": fmt_duration(step.started_at, step.finished_at),
        "status": step.status,
        "status_label": label,
        "status_class": css,
        "error": step.error,
        "output": step.output,
        "output_text": format_output(step.output) if step.output else "",
    }


def run_detail(run: RunRecord) -> dict[str, Any]:
    case = dict(run.case)
    specialty = str(case.get("preferred_specialty") or "")
    case["preferred_specialty_label"] = SPECIALTY_LABELS.get(specialty, specialty or "—")
    return {**run_row(run), "case": case, "trace_id": run.trace_id, "finished_at": run.finished_at}


def event_row(event: Event) -> dict[str, Any]:
    return {
        "ts": event.ts,
        "ts_fmt": fmt_time(event.ts),
        "source": event.source,
        "action": event.action,
        "action_label": ACTION_LABELS.get(event.action, event.action),
        "run_id": event.run_id,
        "run_short": short_id(event.run_id) if event.run_id else "",
        "task_id": event.task_id,
        "step": event.step,
        "step_label": STEP_LABELS.get(event.step, event.step) if event.step else "",
        "trace_id": event.trace_id,
        "span_id": event.span_id,
        "subject": event.subject,
        "summary": event.summary,
    }


def autoscaler_row(raw: dict[str, Any]) -> dict[str, Any]:
    step = str(raw.get("step", ""))
    updated = raw.get("updated_at") or ""
    return {
        "step": step,
        "step_label": STEP_LABELS.get(step, step),
        "backlog": raw.get("backlog", 0),
        "per_instance": raw.get("per_instance", 0),
        "range": f"{raw.get('min', 0)}–{raw.get('max', 0)}",
        "total": raw.get("total", 0),
        "desired": raw.get("desired", 0),
        "auto": ", ".join(str(x) for x in (raw.get("auto") or [])) or "—",
        "updated_fmt": fmt_time(str(updated)) if isinstance(updated, str) else "—",
    }


def metric_rows(metrics: dict[str, Any]) -> list[dict[str, Any]]:
    by_status = metrics.get("runs_by_status", {})
    rows = [
        ("Запусков выполняется", by_status.get("running", 0)),
        ("Запусков завершено", by_status.get("completed", 0)),
        ("Остановлено (экстренные)", by_status.get("stopped", 0)),
        ("Запусков с ошибкой", by_status.get("failed", 0)),
        ("Задач отправлено", metrics.get("tasks", 0)),
        ("Повторных попыток", metrics.get("retries", 0)),
        ("Таймаутов", metrics.get("timeouts", 0)),
        ("Аукционов", metrics.get("auctions", 0)),
    ]
    return [{"name": name, "value": value} for name, value in rows]


def now_str() -> str:
    return datetime.now(UTC).strftime("%H:%M:%S")


# ---- ошибки валидации по-русски ---------------------------------------------------------

FIELD_LABELS = {
    "patient_id": "Пациент",
    "patient_name": "ФИО",
    "age": "Возраст",
    "phone": "Телефон",
    "complaint": "Жалоба",
    "preferred_specialty": "Специальность",
    "appointment_id": "Номер записи",
    "rating": "Оценка",
    "text": "Текст отзыва",
    "limit": "limit",
    "offset": "offset",
    "status": "Статус",
    "count": "Количество",
}


def translate_error(err: dict[str, Any]) -> dict[str, str]:
    """Одна ошибка pydantic/FastAPI → {field, label, message} на русском."""
    loc = [str(p) for p in err.get("loc", ()) if p not in ("body", "query", "path")]
    field = ".".join(loc)
    ctx = err.get("ctx") or {}
    kind = err.get("type", "")
    if kind == "missing":
        message = "обязательное поле"
    elif kind == "string_too_short":
        minimum = ctx.get("min_length")
        message = "обязательное поле" if minimum == 1 else f"слишком коротко (минимум {minimum} симв.)"
    elif kind == "string_too_long":
        message = f"слишком длинно (максимум {ctx.get('max_length')} симв.)"
    elif kind in ("greater_than_equal", "greater_than"):
        message = f"значение не меньше {ctx.get('ge', ctx.get('gt'))}"
    elif kind in ("less_than_equal", "less_than"):
        message = f"значение не больше {ctx.get('le', ctx.get('lt'))}"
    elif kind.startswith("int_") or kind == "int_type":
        message = "должно быть целым числом"
    elif kind.startswith("float_"):
        message = "должно быть числом"
    elif kind == "string_type":
        message = "должно быть строкой"
    elif kind == "literal_error":
        message = f"допустимые значения: {ctx.get('expected', '')}".replace("'", "")
    elif kind in ("json_invalid", "model_attributes_type", "dict_type", "model_type"):
        message = "ожидался корректный JSON-объект"
    elif kind == "value_error":
        message = str(err.get("msg", "")).removeprefix("Value error, ") or "недопустимое значение"
    else:
        message = "недопустимое значение"
    return {"field": field, "label": FIELD_LABELS.get(field, field or "Запрос"), "message": message}


def error_detail(errors: list[dict[str, Any]]) -> dict[str, Any]:
    items = [translate_error(e) for e in errors]
    text = "; ".join(f"{i['label']}: {i['message']}" for i in items)
    return {"detail": text or "Некорректный запрос", "errors": items}

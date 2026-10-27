"""Хранилище событий дашборда: последние N событий в памяти, группировка по run_id."""

from __future__ import annotations

import json
import logging
from collections import deque
from typing import Any

from pydantic import ValidationError

from mas.common.models import Event, parse_ts

log = logging.getLogger("mas.dashboard.events")

NOTIFY_SUBJECT = "notify.patient"


class EventStore:
    """Ограниченная очередь событий (старые вытесняются). Один поток asyncio — блокировки не нужны."""

    def __init__(self, max_events: int = 2000) -> None:
        self._events: deque[Event] = deque(maxlen=max(1, max_events))

    def __len__(self) -> int:
        return len(self._events)

    def add(self, event: Event) -> None:
        self._events.append(event)

    def add_message(self, subject: str, data: bytes) -> None:
        """Разбирает сообщение шины в событие; мусор тихо отбрасывается (шина общая, чужие данные возможны)."""
        event = parse_message(subject, data)
        if event is None:
            log.debug("сообщение в %s не распознано как событие", subject)
            return
        self.add(event)

    def query(self, *, source: str = "", action: str = "", run_id: str = "") -> list[Event]:
        """Подходящие события, новые первыми (порядок поступления)."""
        return [
            e
            for e in reversed(self._events)
            if (not source or e.source == source)
            and (not action or e.action == action)
            and (not run_id or e.run_id == run_id)
        ]

    def for_run(self, run_id: str) -> list[Event]:
        """События запуска по возрастанию времени — трасса «кто что получил/передал».

        Сортировка устойчивая: события одной секунды остаются в порядке поступления.
        """
        events = list(reversed(self.query(run_id=run_id)))
        return sorted(events, key=_sort_key)

    def by_run(self) -> dict[str, list[Event]]:
        """Группировка всех хранимых событий по run_id (события без run_id пропускаются)."""
        groups: dict[str, list[Event]] = {}
        for event in self._events:
            if event.run_id:
                groups.setdefault(event.run_id, []).append(event)
        return groups

    def notifications(self) -> list[Event]:
        return [e for e in reversed(self._events) if e.subject == NOTIFY_SUBJECT]

    def sources(self) -> list[str]:
        return sorted({e.source for e in self._events})


def _sort_key(event: Event) -> float:
    try:
        return parse_ts(event.ts).timestamp()
    except ValueError:
        return 0.0


def parse_message(subject: str, data: bytes) -> Event | None:
    if subject == NOTIFY_SUBJECT:
        return _parse_notification(data)
    try:
        return Event.model_validate_json(data)
    except ValidationError:
        return None


def _parse_notification(data: bytes) -> Event | None:
    """`notify.patient` — не Event, а сообщение «отправлено пациенту» (имитация SMS)."""
    try:
        msg: Any = json.loads(data)
    except ValueError:
        return None
    if not isinstance(msg, dict):
        return None
    kind = {"reminder": "напоминание", "survey": "опрос"}.get(str(msg.get("type", "")), str(msg.get("type", "")))
    parts = [kind, str(msg.get("patient_name") or msg.get("patient_id") or "")]
    channel = msg.get("channel")
    head = " — ".join(p for p in parts if p)
    summary = f"{head} ({channel})" if channel else head
    if msg.get("text"):
        summary = f"{summary}: {msg['text']}"
    try:
        return Event(
            ts=str(msg.get("sent_at") or "") or Event(source="x", action="x").ts,
            source="notify",
            action="notified",
            run_id=str(msg.get("run_id") or ""),
            subject=NOTIFY_SUBJECT,
            summary=summary[:500],
        )
    except ValidationError:
        return None

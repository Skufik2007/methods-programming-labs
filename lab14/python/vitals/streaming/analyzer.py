"""Ядро потокового анализа без NATS: разбор показания, идемпотентность, окно, тревоги, снимок."""

from __future__ import annotations

import math
import time
from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from vitals.streaming.alerts import RULES, AlertEvent, AlertTracker
from vitals.streaming.window import FUTURE, LATE, Agg, SlidingWindow, merge

# Диапазоны и единицы из раздела 2 контракта.
RANGES: dict[str, tuple[float, float, str]] = {
    "heart_rate": (25, 250, "bpm"),
    "spo2": (50, 100, "%"),
    "temperature": (30.0, 43.0, "C"),
    "systolic_bp": (50, 260, "mmHg"),
    "diastolic_bp": (30, 160, "mmHg"),
    "resp_rate": (4, 60, "breaths/min"),
    "steps": (0, 400, "count"),
}


def is_valid(metric: str, value: object, unit: object) -> bool:
    spec = RANGES.get(metric)
    if spec is None or isinstance(value, bool) or not isinstance(value, int | float):
        return False
    if not math.isfinite(value):
        return False
    lo, hi, expected_unit = spec
    return lo <= value <= hi and unit == expected_unit


def parse_ts(s: str) -> float:
    dt = datetime.fromisoformat(s)  # Z понимается с Python 3.11
    if dt.tzinfo is None:
        raise ValueError("ts без часового пояса")
    return dt.timestamp()


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class SeenSet:
    """Ограниченное множество недавно виденных ключей (LRU): память не растёт бесконечно."""

    def __init__(self, capacity: int = 200_000) -> None:
        self.capacity = capacity
        self._d: OrderedDict[tuple[str, int], None] = OrderedDict()

    def add(self, key: tuple[str, int]) -> bool:
        """True - ключ новый; False - уже был (дубликат)."""
        if key in self._d:
            self._d.move_to_end(key)
            return False
        self._d[key] = None
        if len(self._d) > self.capacity:
            self._d.popitem(last=False)
        return True

    def __len__(self) -> int:
        return len(self._d)


class _View:
    """Представление пациента для правил тревог поверх SlidingWindow."""

    def __init__(self, window: SlidingWindow, patient_id: str, metrics: set[str]) -> None:
        self.w, self.pid, self.metrics = window, patient_id, metrics

    def agg(self, metric: str, span_s: int) -> Agg | None:
        return self.w.query((self.pid, metric), span_s)

    def total(self, span_s: int) -> Agg | None:
        return merge([a for m in self.metrics if (a := self.agg(m, span_s))])


class Analyzer:
    def __init__(
        self,
        clock: Callable[[], float] = time.time,
        window: SlidingWindow | None = None,
        seen_capacity: int = 200_000,
        clear_after_s: float = 30.0,
    ) -> None:
        self.clock = clock
        self.window = window or SlidingWindow(clock=clock)
        self.seen = SeenSet(seen_capacity)
        self.tracker = AlertTracker(clear_after_s)
        self._patients: dict[str, set[str]] = {}
        self.counters = {"ok": 0, "duplicate": 0, "bad_message": 0, "late": 0, "future": 0}

    def ingest(self, r: dict[str, Any]) -> str:
        """Обработать одно raw-показание; вернуть исход (ключ счётчика)."""
        try:
            device, seq = str(r["device_id"]), int(r["seq"])
            pid, metric = str(r["patient_id"]), str(r["metric"])
            ts = parse_ts(r["ts"])
        except (KeyError, TypeError, ValueError, AttributeError):
            return self._count("bad_message")
        if not self.seen.add((device, seq)):
            return self._count("duplicate")
        value = r.get("value")
        valid = is_valid(metric, value, r.get("unit"))
        res = self.window.add((pid, metric), ts, float(value) if valid else None, valid)
        if res == LATE:
            return self._count("late")
        if res == FUTURE:
            return self._count("future")
        self._patients.setdefault(pid, set()).add(metric)
        return self._count("ok")

    def _count(self, name: str) -> str:
        self.counters[name] += 1
        return name

    def tick(self) -> list[AlertEvent]:
        """Вычислить правила для всех пациентов; вернуть только смены состояния."""
        now = self.clock()
        events: list[AlertEvent] = []
        for pid, metrics in self._patients.items():
            view = _View(self.window, pid, metrics)
            for name, rule in RULES.items():
                ev = self.tracker.update(pid, name, rule(view), now)
                if ev:
                    events.append(ev)
        self._prune(now)
        return events

    def _prune(self, now: float) -> None:
        self.window.prune(now)
        alive: dict[str, set[str]] = {}
        for pid, m in self.window.keys():
            alive.setdefault(pid, set()).add(m)
        # Пациента с активной тревогой не удаляем, пока тревога не снята (иначе она «зависнет»).
        for pid in list(self._patients):
            if pid in alive:
                self._patients[pid] = alive[pid]
            elif not self.tracker.active_for(pid):
                del self._patients[pid]

    def snapshot(self) -> dict[str, Any]:
        now = self.clock()
        patients = []
        for pid in sorted(self._patients):
            metrics: dict[str, Any] = {}
            for m in sorted(self._patients[pid]):
                a = self.window.query((pid, m))
                if a is None or a.valid == 0:
                    continue
                metrics[m] = {
                    "last": round(a.last, 2),
                    "last_ts": iso(a.last_ts),
                    "mean": round(a.mean, 2),
                    "min": round(a.min, 2),
                    "max": round(a.max, 2),
                    "count": a.count,
                    "invalid_ratio": round(a.invalid_ratio, 3),
                }
            patients.append({"patient_id": pid, "metrics": metrics, "alerts": self.tracker.active_for(pid)})
        return {
            "type": "snapshot",
            "ts": iso(now),
            "patients": patients,
            "counters": dict(self.counters, seen=len(self.seen)),
        }


def alert_message(ev: AlertEvent) -> dict[str, Any]:
    msg: dict[str, Any] = {
        "type": "alert",
        "event": ev.event,
        "alert": ev.alert,
        "patient_id": ev.patient_id,
        "ts": iso(ev.ts),
    }
    if ev.value is not None:
        msg["value"] = round(ev.value, 3)
    return msg

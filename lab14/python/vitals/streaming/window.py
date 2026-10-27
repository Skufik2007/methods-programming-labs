"""Скользящее окно 5 минут с шагом 10 с по ключу (patient_id, metric).

Вместо хранения всех точек - кольцо из окно/шаг (30) «корзин» с частичными агрегатами
(count, invalid, sum, min, max, last). Память на ключ постоянна и не зависит от частоты датчиков.
Время - по `ts` события; текущее время берётся из инжектируемых часов (`clock`).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Hashable
from dataclasses import dataclass
from math import floor

# Результаты add(): что произошло с показанием.
OK = "ok"
LATE = "late"  # старше окна: корзина уже вытеснена из кольца
FUTURE = "future"  # ts дальше «сейчас + допуск» (сбой часов устройства: +1 ч)


@dataclass(frozen=True)
class Agg:
    """Агрегат по интервалу. `count` - все показания (валидные + невалидные), как в agg-контракте."""

    count: int
    invalid: int
    sum: float
    min: float
    max: float
    last: float
    last_ts: float

    @property
    def valid(self) -> int:
        return self.count - self.invalid

    @property
    def mean(self) -> float:
        return self.sum / self.valid if self.valid else float("nan")

    @property
    def invalid_ratio(self) -> float:
        return self.invalid / self.count if self.count else 0.0


class _Bucket:
    __slots__ = ("count", "idx", "invalid", "last", "last_ts", "max", "min", "sum")

    def __init__(self, idx: int) -> None:
        self.idx = idx
        self.count = 0
        self.invalid = 0
        self.sum = 0.0
        self.min = float("inf")
        self.max = float("-inf")
        self.last = 0.0
        self.last_ts = float("-inf")


class _Ring:
    __slots__ = ("head", "slots")

    def __init__(self, n: int) -> None:
        self.slots: list[_Bucket | None] = [None] * n
        self.head = -(10**18)  # наибольший индекс корзины, виденный по этому ключу


def merge(aggs: list[Agg]) -> Agg | None:
    """Объединить частичные агрегаты (используется и для суммы по пациенту)."""
    if not aggs:
        return None
    valid = [a for a in aggs if a.valid > 0]
    newest = max(valid, key=lambda a: a.last_ts) if valid else aggs[0]
    return Agg(
        count=sum(a.count for a in aggs),
        invalid=sum(a.invalid for a in aggs),
        sum=sum(a.sum for a in valid),
        min=min((a.min for a in valid), default=float("nan")),
        max=max((a.max for a in valid), default=float("nan")),
        last=newest.last if valid else float("nan"),
        last_ts=newest.last_ts if valid else float("-inf"),
    )


class SlidingWindow:
    def __init__(
        self,
        window_s: int = 300,
        step_s: int = 10,
        clock: Callable[[], float] = time.time,
        future_tolerance_s: float = 10.0,
    ) -> None:
        if window_s % step_s:
            raise ValueError("window_s должно быть кратно step_s")
        self.window_s, self.step_s, self.clock = window_s, step_s, clock
        self.n = window_s // step_s
        self.future_tolerance_s = future_tolerance_s
        self._rings: dict[Hashable, _Ring] = {}
        self.late_dropped = 0
        self.future_dropped = 0

    def _idx(self, t: float) -> int:
        return floor(t / self.step_s)

    def add(self, key: Hashable, ts: float, value: float | None, valid: bool) -> str:
        now = self.clock()
        if ts > now + self.future_tolerance_s:
            # Не пускаем «будущее»: иначе head кольца ушёл бы вперёд и все нормальные данные стали бы опоздавшими.
            self.future_dropped += 1
            return FUTURE
        idx = self._idx(ts)
        # Опоздавшее: старше окна относительно текущего времени или относительно новейших данных ключа.
        ring = self._rings.get(key)
        head = ring.head if ring else -(10**18)
        if idx <= self._idx(now) - self.n or idx <= head - self.n:
            self.late_dropped += 1
            return LATE
        if ring is None:
            ring = self._rings[key] = _Ring(self.n)
        ring.head = max(ring.head, idx)
        slot = idx % self.n
        b = ring.slots[slot]
        if b is None or b.idx < idx:
            b = ring.slots[slot] = _Bucket(idx)  # прежняя корзина слота старше окна - вытесняем
        b.count += 1
        if not valid or value is None:
            b.invalid += 1
        else:
            b.sum += value
            b.min = min(b.min, value)
            b.max = max(b.max, value)
            if ts >= b.last_ts:  # события могут приходить не по порядку
                b.last, b.last_ts = value, ts
        return OK

    def query(self, key: Hashable, span_s: int | None = None, now: float | None = None) -> Agg | None:
        """Агрегат за последние `span_s` секунд (с точностью до шага), None - данных нет."""
        ring = self._rings.get(key)
        if ring is None:
            return None
        span = self.window_s if span_s is None else min(span_s, self.window_s)
        lo = self._idx(self.clock() if now is None else now) - span // self.step_s + 1
        parts = [
            Agg(b.count, b.invalid, b.sum, b.min, b.max, b.last, b.last_ts)
            for b in ring.slots
            if b is not None and b.idx >= lo
        ]
        return merge(parts)

    def keys(self) -> list[Hashable]:
        return list(self._rings)

    def prune(self, now: float | None = None) -> int:
        """Удалить ключи без данных в окне (иначе ушедшие пациенты занимают память вечно)."""
        lo = self._idx(self.clock() if now is None else now) - self.n + 1
        dead = [k for k, r in self._rings.items() if r.head < lo]
        for k in dead:
            del self._rings[k]
        return len(dead)

"""Tumbling-окна раздела 2.4: полуоткрытые [start, end), выровнены по эпохе.

Чистый класс без ввода-вывода; время сборщика приходит параметром `now_ms` (инжекция часов).
Не потокобезопасен — используется из одного asyncio-потока.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from vitals.pycollector.model import Agg, Reading, valid_value

# насколько ts может опережать часы сборщика, прежде чем это сбой часов устройства
FUTURE_SKEW_MS = 60_000


class AddResult(Enum):
    ACCEPTED = "accepted"
    LATE = "late"  # окно уже закрыто: late_dropped
    DUPLICATE = "duplicate"  # seq уже был — в агрегат не входит


@dataclass
class _State:
    patient: str
    count: int = 0
    invalid: int = 0
    sum: float = 0.0
    min: float = 0.0
    max: float = 0.0
    last: float = 0.0


@dataclass
class WindowStats:
    open: int = 0
    closed: int = 0
    early_closed: int = 0
    late_dropped: int = 0
    dup_skipped: int = 0
    future_clamped: int = 0


class TumblingAggregator:
    def __init__(self, collector_id: str, size_ms: int, lateness_ms: int) -> None:
        self.collector_id = collector_id
        self.size_ms = size_ms
        self.lateness_ms = lateness_ms
        self._open: dict[tuple[str, str, int], _State] = {}
        self._last_seq: dict[str, int] = {}
        self.stats = WindowStats()

    def add(self, r: Reading, now_ms: int) -> AddResult:
        if r.seq <= self._last_seq.get(r.device_id, 0):
            self.stats.dup_skipped += 1
            return AddResult.DUPLICATE
        self._last_seq[r.device_id] = r.seq

        ts = r.ts_ms
        force_invalid = False
        if ts > now_ms + FUTURE_SKEW_MS:
            # ts из будущего: относим к окну «сейчас» и считаем невалидным
            ts = now_ms
            force_invalid = True
            self.stats.future_clamped += 1
        start = ts - ts % self.size_ms  # % в Python неотрицателен для положительного модуля
        if now_ms >= start + self.size_ms + self.lateness_ms:
            self.stats.late_dropped += 1
            return AddResult.LATE
        key = (r.device_id, r.metric, start)
        st = self._open.get(key)
        if st is None:
            st = self._open[key] = _State(r.patient_id)
            self.stats.open += 1
        if force_invalid or not valid_value(r.metric, r.value):
            st.invalid += 1
            return AddResult.ACCEPTED
        v = r.value
        assert v is not None
        if st.count == 0:
            st.min = st.max = v
        else:
            st.min = min(st.min, v)
            st.max = max(st.max, v)
        st.count += 1
        st.sum += v
        st.last = v  # «последнее» — по порядку поступления
        return AddResult.ACCEPTED

    def close_due(self, now_ms: int) -> list[Agg]:
        """Закрыть окна с now >= window_end + lateness."""
        limit = self.size_ms + self.lateness_ms
        keys = [k for k in self._open if now_ms >= k[2] + limit]
        return self._close(keys, now_ms, count_early=False)

    def close_all(self, now_ms: int) -> list[Agg]:
        """Закрыть всё (graceful shutdown); не дожившие до срока считаются закрытыми досрочно."""
        return self._close(list(self._open), now_ms, count_early=True)

    def _close(self, keys: list[tuple[str, str, int]], now_ms: int, count_early: bool) -> list[Agg]:
        out: list[Agg] = []
        for k in keys:
            st = self._open.pop(k)
            device, metric, start = k
            if count_early and now_ms < start + self.size_ms + self.lateness_ms:
                self.stats.early_closed += 1
            out.append(
                Agg(start, start + self.size_ms, device, st.patient, metric, st.count, st.invalid,
                    st.sum, st.min, st.max, st.last, self.collector_id)
            )
        self.stats.open -= len(out)
        self.stats.closed += len(out)
        out.sort(key=lambda a: (a.window_start_ms, a.device_id, a.metric))
        return out

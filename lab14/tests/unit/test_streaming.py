"""Тесты vitals.streaming: скользящее окно на фейковых часах, правила тревог, гистерезис, идемпотентность."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from vitals.streaming.alerts import (
    RULES,
    AlertTracker,
    rule_desaturation,
    rule_fever,
    rule_hypertension,
    rule_sensor_fault,
    rule_tachycardia,
)
from vitals.streaming.analyzer import Analyzer, SeenSet, parse_ts
from vitals.streaming.window import FUTURE, LATE, OK, Agg, SlidingWindow, merge


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


KEY = ("p-1", "heart_rate")


# --- скользящее окно --------------------------------------------------------------------------------------------


def test_window_size_must_be_multiple_of_step():
    with pytest.raises(ValueError):
        SlidingWindow(window_s=305, step_s=10)


def test_window_aggregates_valid_values_and_counts_invalid_separately():
    clock = FakeClock(1000)
    w = SlidingWindow(clock=clock)
    assert w.add(KEY, 995, 60.0, True) == OK
    assert w.add(KEY, 996, 200.0, False) == OK  # невалидное: в count и invalid, но не в сумму
    a = w.query(KEY)
    assert a is not None
    assert (a.count, a.invalid, a.valid) == (2, 1, 1)
    assert a.mean == 60.0
    assert a.invalid_ratio == 0.5


def test_old_buckets_leave_the_window_on_query():
    clock = FakeClock(1290)
    w = SlidingWindow(clock=clock)
    w.add(KEY, 1000, 100.0, True)  # корзина 100
    assert w.query(KEY, now=1290).count == 1  # 1290 -> lo=100, корзина ещё внутри
    assert w.query(KEY, now=1301) is None  # lo=101, корзина вытеснена


def test_ring_keeps_only_last_window_worth_of_buckets():
    clock = FakeClock(0)
    w = SlidingWindow(clock=clock)
    for t in range(0, 3600, 10):  # час данных, по одному значению на шаг; часы идут вместе с данными
        clock.now = t
        w.add(KEY, t, 1.0, True)
    a = w.query(KEY, now=3590)
    assert a is not None and a.count == 30  # 300 с / 10 с


def test_late_event_older_than_window_is_dropped_and_counted():
    clock = FakeClock(2000)
    w = SlidingWindow(clock=clock)
    assert w.add(KEY, 1000, 60.0, True) == LATE
    assert w.late_dropped == 1
    assert w.query(KEY) is None


def test_late_relative_to_newest_data_of_key_is_dropped():
    clock = FakeClock(1500)
    w = SlidingWindow(clock=clock)
    assert w.add(KEY, 1500, 60.0, True) == OK
    assert w.add(KEY, 1100, 60.0, True) == LATE  # idx 110 <= head 150 - 30
    assert w.late_dropped == 1


def test_future_event_beyond_tolerance_is_rejected_and_boundary_is_accepted():
    clock = FakeClock(1000)
    w = SlidingWindow(clock=clock, future_tolerance_s=10.0)
    assert w.add(KEY, 1011, 60.0, True) == FUTURE
    assert w.add(KEY, 1010, 60.0, True) == OK  # ровно на границе допуска
    assert w.future_dropped == 1


def test_out_of_order_inside_bucket_keeps_newest_last_value():
    clock = FakeClock(1000)
    w = SlidingWindow(clock=clock)
    w.add(KEY, 1005, 70.0, True)
    w.add(KEY, 1002, 50.0, True)  # пришло позже, но старше по ts
    a = w.query(KEY)
    assert a is not None and a.last == 70.0 and a.last_ts == 1005
    assert a.min == 50.0 and a.max == 70.0


def test_prune_removes_keys_without_data_in_window():
    clock = FakeClock(1000)
    w = SlidingWindow(clock=clock)
    w.add(("gone", "heart_rate"), 1000, 60.0, True)
    clock.now = 1290
    w.add(("alive", "heart_rate"), 1290, 60.0, True)
    removed = w.prune(now=1400)
    assert removed == 1
    assert w.keys() == [("alive", "heart_rate")]


def test_merge_of_nothing_is_none_and_merge_sums_counts():
    assert merge([]) is None
    a = Agg(count=2, invalid=0, sum=10.0, min=4.0, max=6.0, last=6.0, last_ts=2.0)
    b = Agg(count=1, invalid=1, sum=0.0, min=0.0, max=0.0, last=0.0, last_ts=-1.0)
    m = merge([a, b])
    assert m is not None
    assert (m.count, m.invalid, m.sum, m.last) == (3, 1, 10.0, 6.0)


# --- правила тревог на граничных значениях ----------------------------------------------------------------------


class FakeView:
    def __init__(self, aggs: dict[tuple[str, int], Agg], total: Agg | None = None) -> None:
        self.aggs = aggs
        self._total = total

    def agg(self, metric: str, span_s: int) -> Agg | None:
        return self.aggs.get((metric, span_s))

    def total(self, span_s: int) -> Agg | None:
        return self._total


def agg(count: int, mean_sum: float, lo: float = 0.0, hi: float = 0.0, last: float = 0.0, invalid: int = 0) -> Agg:
    return Agg(count=count, invalid=invalid, sum=mean_sum, min=lo, max=hi, last=last, last_ts=0.0)


def test_tachycardia_threshold_is_strict_120():
    assert rule_tachycardia(FakeView({("heart_rate", 60): agg(3, 360.0)})) is None
    assert rule_tachycardia(FakeView({("heart_rate", 60): agg(3, 361.0)})) == pytest.approx(361 / 3)


def test_tachycardia_needs_three_samples():
    assert rule_tachycardia(FakeView({("heart_rate", 60): agg(2, 300.0)})) is None


def test_desaturation_threshold_is_strict_below_92():
    assert rule_desaturation(FakeView({("spo2", 60): agg(1, 92.0, lo=92.0)})) is None
    assert rule_desaturation(FakeView({("spo2", 60): agg(1, 91.9, lo=91.9)})) == 91.9


def test_fever_uses_last_value_strictly_above_38():
    assert rule_fever(FakeView({("temperature", 60): agg(1, 38.0, last=38.0)})) is None
    assert rule_fever(FakeView({("temperature", 60): agg(1, 38.1, last=38.1)})) == 38.1


def test_hypertension_threshold_is_strict_160_over_five_minutes():
    assert rule_hypertension(FakeView({("systolic_bp", 300): agg(3, 480.0)})) is None
    assert rule_hypertension(FakeView({("systolic_bp", 300): agg(3, 480.3)})) is not None


def test_sensor_fault_threshold_is_strict_20_percent_and_needs_20_samples():
    twenty = agg(20, 0.0, invalid=4)  # ровно 20%
    assert rule_sensor_fault(FakeView({}, total=twenty)) is None
    assert rule_sensor_fault(FakeView({}, total=agg(20, 0.0, invalid=5))) == 0.25
    assert rule_sensor_fault(FakeView({}, total=agg(19, 0.0, invalid=19))) is None


def test_rules_registry_has_all_five_alerts():
    assert set(RULES) == {"tachycardia", "desaturation", "fever", "hypertension", "sensor_fault"}


# --- гистерезис --------------------------------------------------------------------------------------------------


def test_alert_raised_once_and_not_duplicated_while_active():
    tr = AlertTracker(clear_after_s=30)
    first = tr.update("p-1", "tachycardia", 130.0, now=0)
    assert first is not None and first.event == "raised"
    assert tr.update("p-1", "tachycardia", 131.0, now=10) is None


def test_alert_cleared_only_after_30s_without_condition():
    tr = AlertTracker(clear_after_s=30)
    tr.update("p-1", "tachycardia", 130.0, now=0)
    assert tr.update("p-1", "tachycardia", None, now=10) is None  # условие ложно с t=10
    assert tr.update("p-1", "tachycardia", None, now=39) is None  # 29 с - ещё не снимаем
    cleared = tr.update("p-1", "tachycardia", None, now=40)  # ровно 30 с
    assert cleared is not None and cleared.event == "cleared"
    assert tr.active() == []


def test_short_gap_does_not_clear_and_does_not_reraise():
    tr = AlertTracker(clear_after_s=30)
    tr.update("p-1", "tachycardia", 130.0, now=0)
    tr.update("p-1", "tachycardia", None, now=10)
    assert tr.update("p-1", "tachycardia", 125.0, now=20) is None  # снова истинно: без новой тревоги
    assert tr.update("p-1", "tachycardia", None, now=25) is None
    assert tr.update("p-1", "tachycardia", None, now=50) is None  # 25 с с последнего ложного - не снято
    assert tr.update("p-1", "tachycardia", None, now=55) is not None


def test_clear_for_alert_never_raised_is_silent():
    tr = AlertTracker()
    assert tr.update("p-1", "fever", None, now=100) is None


# --- анализатор: идемпотентность и выдача тревог ----------------------------------------------------------------


def reading(seq: int, ts: str, value: Any = 72.0, metric: str = "heart_rate", unit: str = "bpm",
            device: str = "dev-0001", patient: str = "p-0001") -> dict[str, Any]:
    return {"device_id": device, "patient_id": patient, "seq": seq, "ts": ts, "metric": metric,
            "value": value, "unit": unit, "battery": 80}


BASE = datetime(2026, 10, 13, 9, 30, tzinfo=UTC).timestamp()


def ts_at(offset: float) -> str:
    return datetime.fromtimestamp(BASE + offset, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def test_same_device_and_seq_is_idempotent_even_with_other_payload():
    clock = FakeClock(BASE)
    a = Analyzer(clock=clock)
    assert a.ingest(reading(1, ts_at(0))) == "ok"
    assert a.ingest(reading(1, ts_at(0))) == "duplicate"
    assert a.ingest(reading(1, ts_at(1), value=99.0)) == "duplicate"  # ключ (device_id, seq) один
    assert a.counters["ok"] == 1 and a.counters["duplicate"] == 2


def test_seq_of_other_device_is_not_a_duplicate():
    a = Analyzer(clock=FakeClock(BASE))
    a.ingest(reading(1, ts_at(0), device="dev-0001"))
    assert a.ingest(reading(1, ts_at(0), device="dev-0002")) == "ok"


def test_bad_messages_are_counted_not_raised():
    a = Analyzer(clock=FakeClock(BASE))
    assert a.ingest({"device_id": "d"}) == "bad_message"
    assert a.ingest(reading(1, "не дата")) == "bad_message"
    assert a.ingest(reading(2, "2026-10-13T09:30:00")) == "bad_message"  # без часового пояса
    assert a.counters["bad_message"] == 3


def test_invalid_reading_counts_towards_window_invalid_ratio():
    a = Analyzer(clock=FakeClock(BASE))
    a.ingest(reading(1, ts_at(0), value=72.0))
    a.ingest(reading(2, ts_at(1), value=999.0))  # вне диапазона
    a.ingest(reading(3, ts_at(2), value=72.0, unit="BPM"))  # неверная единица
    snap = a.snapshot()["patients"][0]["metrics"]["heart_rate"]
    assert snap["count"] == 3
    assert snap["invalid_ratio"] == pytest.approx(0.667, abs=1e-3)
    assert snap["mean"] == 72.0


def test_late_and_future_readings_are_counted_by_outcome():
    a = Analyzer(clock=FakeClock(BASE))
    assert a.ingest(reading(1, ts_at(-3600))) == "late"
    assert a.ingest(reading(2, ts_at(3600))) == "future"
    assert a.counters["late"] == 1 and a.counters["future"] == 1


def test_tachycardia_raised_once_by_tick_then_cleared_after_quiet_period():
    clock = FakeClock(BASE)
    a = Analyzer(clock=clock)
    for i in range(4):
        a.ingest(reading(i + 1, ts_at(i), value=130.0))
    clock.now = BASE + 4
    events = a.tick()
    assert [(e.event, e.alert) for e in events] == [("raised", "tachycardia")]
    clock.now = BASE + 5
    assert a.tick() == []  # повтора нет
    # показания перестают приходить: окно пустеет, условие ложно, через 30 с снимаем
    clock.now = BASE + 400
    a.window.prune(clock.now)
    assert [(e.event, e.alert) for e in a.tick()] == []  # ушедший пациент ещё в трекере
    clock.now = BASE + 440
    assert [(e.event, e.alert) for e in a.tick()] == [("cleared", "tachycardia")]


def test_seen_set_evicts_oldest_beyond_capacity():
    s = SeenSet(capacity=2)
    assert s.add(("d", 1)) is True
    assert s.add(("d", 2)) is True
    assert s.add(("d", 1)) is False  # уже есть, становится самым свежим
    assert s.add(("d", 3)) is True  # вытесняет ("d", 2)
    assert s.add(("d", 2)) is True
    assert len(s) == 2


def test_parse_ts_requires_timezone_and_understands_z():
    assert parse_ts("2026-10-13T09:30:00.000Z") == BASE
    with pytest.raises(ValueError):
        parse_ts("2026-10-13T09:30:00")

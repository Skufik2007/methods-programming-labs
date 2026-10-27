"""Скрытые тесты задачи C1 (подбор ближайших окон). Модели их не видят.

Проверяемая реализация задаётся переменной окружения SLOTS_IMPL — путь к файлу
slots.py; по умолчанию — эталон reference_slots.py:

    SLOTS_IMPL=comparison/task6/solutions/opus/slots.py pytest comparison/task6 -q
"""

import importlib.util
import os
import random
import sys
import time as clock
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

_IMPL = Path(os.environ.get("SLOTS_IMPL", Path(__file__).with_name("reference_slots.py")))
_spec = importlib.util.spec_from_file_location("slots_impl", _IMPL)
slots = importlib.util.module_from_spec(_spec)
sys.modules["slots_impl"] = slots  # нужно dataclass-ам при отложенных аннотациях
_spec.loader.exec_module(slots)  # type: ignore[union-attr]

MSK = ZoneInfo("Europe/Moscow")
BERLIN = ZoneInfo("Europe/Berlin")
MON = date(2026, 10, 12)  # понедельник
WEEKDAYS = frozenset({1, 2, 3, 4, 5})


def doctor(id_: int = 1, specialty: str = "Терапевт", **kw) -> "slots.Doctor":
    params = {
        "work_days": WEEKDAYS,
        "work_start": time(9),
        "work_end": time(18),
        "slot_minutes": 30,
    }
    params.update(kw)
    return slots.Doctor(id=id_, specialty=specialty, **params)


def at(day: date, hh: int, mm: int = 0, tz: ZoneInfo = MSK) -> datetime:
    return datetime.combine(day, time(hh, mm), tzinfo=tz)


def find(doctors, busy=None, patient_busy=None, *, now, tz=MSK, specialty="Терапевт", **kw):
    return slots.find_earliest_options(
        doctors, busy or {}, patient_busy or [], specialty=specialty, now=now, tz=tz, **kw
    )


def starts_local(options, tz=MSK):
    return [o.starts_at.astimezone(tz).strftime("%a %H:%M") for o in options]


# --- базовое поведение ------------------------------------------------------


def test_first_slot_is_strictly_after_now():
    result = find([doctor()], now=at(MON, 9), limit=3)
    assert starts_local(result) == ["Mon 09:30", "Mon 10:00", "Mon 10:30"]


def test_now_between_slots():
    result = find([doctor()], now=at(MON, 12, 10), limit=2)
    assert starts_local(result) == ["Mon 12:30", "Mon 13:00"]


def test_after_shift_moves_to_next_working_day():
    result = find([doctor()], now=at(MON, 18), limit=1)
    assert starts_local(result) == ["Tue 09:00"]


def test_weekend_is_skipped():
    friday_evening = at(date(2026, 10, 16), 19)
    result = find([doctor()], now=friday_evening, limit=1)
    assert result[0].starts_at == at(date(2026, 10, 19), 9).astimezone(UTC)


def test_results_are_aware_utc_and_have_correct_end():
    option = find([doctor()], now=at(MON, 9), limit=1, duration_slots=2)[0]
    assert option.starts_at.utcoffset() == timedelta(0)
    assert option.ends_at.utcoffset() == timedelta(0)
    assert option.ends_at - option.starts_at == timedelta(minutes=60)
    assert option.doctor_id == 1


def test_specialty_match_ignores_case_and_spaces():
    result = find([doctor(specialty="  терапевт ")], now=at(MON, 9), specialty="ТЕРАПЕВТ", limit=1)
    assert len(result) == 1


def test_other_specialty_gives_empty_list():
    assert find([doctor(specialty="Хирург")], now=at(MON, 9)) == []


def test_sorted_by_time_then_doctor_id():
    result = find([doctor(2), doctor(1)], now=at(MON, 9), limit=4)
    assert [(o.starts_at, o.doctor_id) for o in result] == sorted((o.starts_at, o.doctor_id) for o in result)
    assert [o.doctor_id for o in result[:2]] == [1, 2]


def test_limit_is_respected():
    assert len(find([doctor()], now=at(MON, 9), limit=7)) == 7


def test_slot_must_fit_into_shift():
    doc = doctor(work_start=time(9), work_end=time(10, 15))
    result = find([doc], now=at(MON, 8), limit=10, horizon_days=1)
    assert starts_local(result) == ["Mon 09:00", "Mon 09:30"]


def test_window_ending_exactly_at_shift_end_is_allowed():
    doc = doctor(work_start=time(9), work_end=time(10))
    result = find([doc], now=at(MON, 8), duration_slots=2, limit=5, horizon_days=1)
    assert starts_local(result) == ["Mon 09:00"]


def test_grid_starts_from_work_start():
    doc = doctor(work_start=time(9, 10), slot_minutes=20)
    result = find([doc], now=at(MON, 8), limit=3)
    assert starts_local(result) == ["Mon 09:10", "Mon 09:30", "Mon 09:50"]


# --- занятость, перерывы, выходные ------------------------------------------


def test_doctor_busy_interval_is_excluded_and_touching_is_allowed():
    busy = {1: [(at(MON, 10), at(MON, 11))]}
    result = find([doctor()], busy, now=at(MON, 9), limit=4)
    assert starts_local(result) == ["Mon 09:30", "Mon 11:00", "Mon 11:30", "Mon 12:00"]


def test_busy_of_other_doctor_does_not_matter():
    busy = {2: [(at(MON, 9), at(MON, 18))]}
    assert starts_local(find([doctor()], busy, now=at(MON, 9), limit=1)) == ["Mon 09:30"]


def test_multi_slot_window_needs_all_slots_free():
    busy = {1: [(at(MON, 10, 30), at(MON, 11))]}
    result = find([doctor()], busy, now=at(MON, 9), duration_slots=2, limit=3)
    assert starts_local(result) == ["Mon 09:30", "Mon 11:00", "Mon 11:30"]


def test_patient_busy_interval_is_excluded():
    result = find([doctor()], patient_busy=[(at(MON, 9, 30), at(MON, 10, 15))], now=at(MON, 9), limit=2)
    assert starts_local(result) == ["Mon 10:30", "Mon 11:00"]


def test_busy_in_other_timezone_is_compared_correctly():
    plus5 = ZoneInfo("Asia/Yekaterinburg")  # UTC+5: 12:30 там = 10:30 МСК
    busy = {1: [(datetime(2026, 10, 12, 12, 30, tzinfo=plus5), datetime(2026, 10, 12, 13, 0, tzinfo=plus5))]}
    result = find([doctor()], busy, now=at(MON, 10), limit=2)
    assert starts_local(result) == ["Mon 11:00", "Mon 11:30"]


def test_breaks_are_excluded_and_window_may_end_at_break_start():
    doc = doctor(breaks=((time(13), time(14)),))
    result = find([doc], now=at(MON, 12), limit=3)
    assert starts_local(result) == ["Mon 12:30", "Mon 14:00", "Mon 14:30"]


def test_window_crossing_break_is_excluded():
    doc = doctor(breaks=((time(13), time(14)),))
    result = find([doc], now=at(MON, 12), duration_slots=2, limit=2)
    assert starts_local(result) == ["Mon 14:00", "Mon 14:30"]


def test_days_off_are_skipped():
    doc = doctor(days_off=frozenset({MON, date(2026, 10, 13)}))
    result = find([doc], now=at(MON, 8), limit=1)
    assert starts_local(result) == ["Wed 09:00"]


# --- горизонт ---------------------------------------------------------------


def test_horizon_upper_bound_is_inclusive():
    now = at(MON, 9)
    result = find([doctor(work_days=frozenset({1}))], now=now, horizon_days=7, limit=50)
    assert result[-1].starts_at == (now + timedelta(days=7)).astimezone(UTC)


def test_nothing_beyond_horizon():
    now = at(MON, 9)
    result = find([doctor()], now=now, horizon_days=1, limit=100)
    assert all(o.starts_at <= now + timedelta(days=1) for o in result)
    assert starts_local(result)[-1] == "Tue 09:00"


# --- ошибки входных данных --------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"now": datetime(2026, 10, 12, 9)},
        {"duration_slots": 0},
        {"duration_slots": 5},
        {"limit": 0},
        {"horizon_days": 0},
    ],
    ids=["naive-now", "duration-0", "duration-5", "limit-0", "horizon-0"],
)
def test_invalid_arguments_raise_value_error(kwargs):
    params = {"now": at(MON, 9)} | kwargs
    with pytest.raises(ValueError):
        find([doctor()], **params)


@pytest.mark.parametrize(
    ("busy", "patient_busy"),
    [
        ({1: [(datetime(2026, 10, 12, 10), datetime(2026, 10, 12, 11))]}, []),
        ({}, [(datetime(2026, 10, 12, 10), datetime(2026, 10, 12, 11))]),
        ({1: [(at(MON, 11), at(MON, 10))]}, []),
        ({}, [(at(MON, 10), at(MON, 10))]),
    ],
    ids=["naive-busy", "naive-patient", "inverted-busy", "empty-patient"],
)
def test_invalid_intervals_raise_value_error(busy, patient_busy):
    with pytest.raises(ValueError):
        find([doctor()], busy, patient_busy, now=at(MON, 9))


# --- переход на летнее и зимнее время ---------------------------------------


def _night_doctor():
    return doctor(work_days=frozenset(range(1, 8)), work_start=time(0), work_end=time(5), slot_minutes=60)


def test_spring_forward_skips_nonexistent_wall_time():
    day = date(2026, 3, 29)  # в Берлине 02:00 -> 03:00
    now = datetime(2026, 3, 28, 12, tzinfo=BERLIN)
    result = find([_night_doctor()], now=now, tz=BERLIN, limit=10, horizon_days=1)
    local = [o.starts_at.astimezone(BERLIN) for o in result if o.starts_at.astimezone(BERLIN).date() == day]
    hours = [d.hour for d in local]
    assert 2 not in hours
    assert {0, 3, 4} <= set(hours)
    assert len({o.starts_at for o in result}) == len(result)


def test_fall_back_takes_ambiguous_wall_time_once_with_fold_0():
    day = date(2026, 10, 25)  # в Берлине 03:00 -> 02:00
    now = datetime(2026, 10, 24, 12, tzinfo=BERLIN)
    result = find([_night_doctor()], now=now, tz=BERLIN, limit=10, horizon_days=1)
    on_day = [o for o in result if o.starts_at.astimezone(BERLIN).date() == day]
    hours = [o.starts_at.astimezone(BERLIN).hour for o in on_day]
    assert hours.count(2) == 1
    two_am = next(o for o in on_day if o.starts_at.astimezone(BERLIN).hour == 2)
    assert two_am.starts_at == datetime(2026, 10, 25, 0, 0, tzinfo=UTC)  # 02:00 CEST


# --- производительность -----------------------------------------------------


def test_performance_50_doctors_200_busy_each():
    rnd = random.Random(42)
    doctors = [doctor(i, slot_minutes=rnd.choice([15, 20, 30])) for i in range(1, 51)]
    base = at(MON, 9)
    busy = {}
    for d in doctors:
        items = []
        for _ in range(200):
            start = base + timedelta(minutes=15 * rnd.randrange(0, 14 * 24 * 4))
            items.append((start, start + timedelta(minutes=15)))
        busy[d.id] = items
    started = clock.perf_counter()
    result = find(doctors, busy, now=base, limit=1000, horizon_days=14)
    elapsed = clock.perf_counter() - started
    assert len(result) == 1000
    assert elapsed < 1.0, f"{elapsed:.2f} s"

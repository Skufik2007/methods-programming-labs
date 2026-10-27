"""Подбор ближайших свободных окон для записи к врачу.

Только стандартная библиотека, Python 3.12.
"""
from __future__ import annotations

import heapq
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from itertools import islice
from typing import Iterable, Iterator
from zoneinfo import ZoneInfo

__all__ = ["Doctor", "Option", "find_earliest_options"]

UTC = timezone.utc


@dataclass(frozen=True)
class Doctor:
    id: int
    specialty: str
    work_days: frozenset[int]          # ISO: 1 = пн ... 7 = вс
    work_start: time                   # местное время клиники
    work_end: time                     # местное время клиники, work_start < work_end
    slot_minutes: int                  # шаг сетки, > 0
    breaks: tuple[tuple[time, time], ...] = ()   # перерывы [начало, конец), местное время
    days_off: frozenset[date] = frozenset()      # отпуск / выходные даты (местные)


@dataclass(frozen=True)
class Option:
    doctor_id: int
    starts_at: datetime                # aware, UTC
    ends_at: datetime                  # aware, UTC


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def _is_aware(dt: datetime) -> bool:
    return dt.tzinfo is not None and dt.utcoffset() is not None


def _check_aware(dt: object, what: str) -> datetime:
    if not isinstance(dt, datetime):
        raise ValueError(f"{what}: ожидался datetime, получено {type(dt).__name__}")
    if not _is_aware(dt):
        raise ValueError(f"{what}: naive datetime недопустим, нужен datetime с часовым поясом ({dt!r})")
    return dt


def _normalize_intervals(
    intervals: Iterable[tuple[datetime, datetime]], what: str
) -> list[tuple[datetime, datetime]]:
    """Проверяет интервалы и переводит их в UTC."""
    result: list[tuple[datetime, datetime]] = []
    for idx, interval in enumerate(intervals):
        try:
            s, e = interval
        except (TypeError, ValueError):
            raise ValueError(f"{what}[{idx}]: ожидалась пара (начало, конец), получено {interval!r}") from None
        _check_aware(s, f"{what}[{idx}].начало")
        _check_aware(e, f"{what}[{idx}].конец")
        su, eu = s.astimezone(UTC), e.astimezone(UTC)
        if eu <= su:
            raise ValueError(f"{what}[{idx}]: конец интервала ({e.isoformat()}) не позже начала ({s.isoformat()})")
        result.append((su, eu))
    return result


class _IntervalSet:
    """Множество полуоткрытых интервалов [s, e) с проверкой пересечения за O(log n)."""

    __slots__ = ("_starts", "_ends")

    def __init__(self, intervals: list[tuple[datetime, datetime]]) -> None:
        merged: list[list[datetime]] = []
        for s, e in sorted(intervals):
            if merged and s <= merged[-1][1]:
                # Пересекающиеся или касающиеся интервалы сливаем:
                # для полуоткрытых интервалов это не меняет ответа на вопрос о пересечении.
                if e > merged[-1][1]:
                    merged[-1][1] = e
            else:
                merged.append([s, e])
        self._starts = [m[0] for m in merged]
        self._ends = [m[1] for m in merged]

    def overlaps(self, s: datetime, e: datetime) -> bool:
        # Первый интервал, чей конец строго больше s (касание s == end — не пересечение).
        i = bisect_right(self._ends, s)
        return i < len(self._starts) and self._starts[i] < e


def _local_to_utc(wall: datetime, tz: ZoneInfo) -> datetime | None:
    """Переводит «настенное» naive-время клиники в UTC.

    Несуществующее время (переход на летнее) -> None.
    Повторяющееся время (переход на зимнее) -> первое вхождение (fold=0).
    """
    aware = wall.replace(tzinfo=tz, fold=0)
    utc = aware.astimezone(UTC)
    # Обратное преобразование не совпадает только для времени из «дыры».
    if utc.astimezone(tz).replace(tzinfo=None) != wall:
        return None
    return utc


def _norm_specialty(s: str) -> str:
    return s.strip().casefold()


def _validate_doctor(doc: Doctor) -> None:
    if not isinstance(doc.slot_minutes, int) or isinstance(doc.slot_minutes, bool) or doc.slot_minutes <= 0:
        raise ValueError(f"Врач {doc.id}: slot_minutes должен быть целым > 0, получено {doc.slot_minutes!r}")
    if not doc.work_start < doc.work_end:
        raise ValueError(f"Врач {doc.id}: work_start ({doc.work_start}) должен быть раньше work_end ({doc.work_end})")
    for idx, (bs, be) in enumerate(doc.breaks):
        if not bs < be:
            raise ValueError(f"Врач {doc.id}: перерыв #{idx}: конец ({be}) не позже начала ({bs})")


# ---------------------------------------------------------------------------
# Генерация окон для одного врача (в порядке возрастания starts_at)
# ---------------------------------------------------------------------------

def _doctor_options(
    doc: Doctor,
    doc_busy: _IntervalSet,
    patient_busy: _IntervalSet,
    *,
    now_utc: datetime,
    horizon_utc: datetime,
    tz: ZoneInfo,
    duration_slots: int,
) -> Iterator[Option]:
    step = timedelta(minutes=doc.slot_minutes)
    duration = step * duration_slots

    first_day = now_utc.astimezone(tz).date()
    last_day = horizon_utc.astimezone(tz).date()

    day = first_day
    one_day = timedelta(days=1)
    while day <= last_day:
        if day.isoweekday() in doc.work_days and day not in doc.days_off:
            shift_start = datetime.combine(day, doc.work_start)
            shift_end = datetime.combine(day, doc.work_end)
            day_breaks = [
                (datetime.combine(day, bs), datetime.combine(day, be)) for bs, be in doc.breaks
            ]

            wall = shift_start
            while wall + duration <= shift_end:          # окно целиком внутри смены
                wall_end = wall + duration
                cur = wall
                wall += step

                # Перерывы — в местном «настенном» времени, полуоткрыто.
                if any(bs < wall_end and cur < be for bs, be in day_breaks):
                    continue

                start_utc = _local_to_utc(cur, tz)
                if start_utc is None:                     # несуществующее время
                    continue
                if start_utc <= now_utc:                  # строго после now
                    continue
                if start_utc > horizon_utc:               # дальше — только позже
                    return
                end_utc = start_utc + duration

                if doc_busy.overlaps(start_utc, end_utc):
                    continue
                if patient_busy.overlaps(start_utc, end_utc):
                    continue

                yield Option(doctor_id=doc.id, starts_at=start_utc, ends_at=end_utc)
        day += one_day


# ---------------------------------------------------------------------------
# Публичная функция
# ---------------------------------------------------------------------------

def find_earliest_options(
    doctors: list[Doctor],
    busy: dict[int, list[tuple[datetime, datetime]]],
    patient_busy: list[tuple[datetime, datetime]],
    *,
    specialty: str,
    now: datetime,
    tz: ZoneInfo,
    duration_slots: int = 1,
    limit: int = 5,
    horizon_days: int = 14,
) -> list[Option]:
    """Возвращает до `limit` ближайших свободных окон, отсортированных по (starts_at, doctor_id)."""
    # --- валидация параметров ---
    _check_aware(now, "now")
    for name, value in (("duration_slots", duration_slots), ("limit", limit), ("horizon_days", horizon_days)):
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"{name} должен быть целым числом, получено {value!r}")
    if not 1 <= duration_slots <= 4:
        raise ValueError(f"duration_slots должен быть в диапазоне 1..4, получено {duration_slots}")
    if limit < 1:
        raise ValueError(f"limit должен быть >= 1, получено {limit}")
    if horizon_days < 1:
        raise ValueError(f"horizon_days должен быть >= 1, получено {horizon_days}")

    # --- валидация и нормализация интервалов (всех, а не только нужных врачей) ---
    busy_utc = {
        doc_id: _normalize_intervals(intervals, f"busy[{doc_id}]")
        for doc_id, intervals in busy.items()
    }
    patient_set = _IntervalSet(_normalize_intervals(patient_busy, "patient_busy"))

    now_utc = now.astimezone(UTC)
    horizon_utc = now_utc + timedelta(days=horizon_days)

    # --- выбор врачей ---
    wanted = _norm_specialty(specialty)
    generators: list[Iterator[Option]] = []
    for doc in doctors:
        if _norm_specialty(doc.specialty) != wanted:
            continue
        _validate_doctor(doc)
        generators.append(
            _doctor_options(
                doc,
                _IntervalSet(busy_utc.get(doc.id, [])),
                patient_set,
                now_utc=now_utc,
                horizon_utc=horizon_utc,
                tz=tz,
                duration_slots=duration_slots,
            )
        )

    # --- ленивое k-путевое слияние отсортированных потоков ---
    merged = heapq.merge(*generators, key=lambda o: (o.starts_at, o.doctor_id))
    return list(islice(merged, limit))

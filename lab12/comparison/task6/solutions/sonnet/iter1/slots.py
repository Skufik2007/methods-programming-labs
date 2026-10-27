"""Подбор ближайших окон для записи к врачу (только стандартная библиотека)."""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

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


# ---------------------------------------------------------------- проверки

def _is_aware(dt: datetime) -> bool:
    return dt.tzinfo is not None and dt.utcoffset() is not None


def _check_intervals(intervals, what: str) -> list[tuple[datetime, datetime]]:
    """Проверяет интервалы и возвращает их в UTC."""
    result = []
    for item in intervals:
        try:
            s, e = item
        except (TypeError, ValueError):
            raise ValueError(f"{what}: интервал должен быть парой (начало, конец): {item!r}")
        if not isinstance(s, datetime) or not isinstance(e, datetime):
            raise ValueError(f"{what}: границы интервала должны быть datetime: {item!r}")
        if not _is_aware(s) or not _is_aware(e):
            raise ValueError(f"{what}: naive datetime в интервале ({s!r}, {e!r}); нужен aware")
        if e <= s:
            raise ValueError(f"{what}: конец интервала не позже начала ({s!r}, {e!r})")
        result.append((s.astimezone(UTC), e.astimezone(UTC)))
    return result


def _merge(intervals: list[tuple[datetime, datetime]]):
    """Сливает интервалы в непересекающиеся (полуоткрытые, касающиеся тоже сливаются).

    Возвращает два параллельных отсортированных списка: начала и концы.
    """
    intervals = sorted(intervals)
    starts: list[datetime] = []
    ends: list[datetime] = []
    for s, e in intervals:
        if ends and s <= ends[-1]:
            if e > ends[-1]:
                ends[-1] = e
        else:
            starts.append(s)
            ends.append(e)
    return starts, ends


def _overlaps(merged, s: datetime, e: datetime) -> bool:
    """Пересекается ли [s, e) с набором слитых интервалов. O(log n)."""
    starts, ends = merged
    # первый интервал, чей конец строго позже s (касание не пересечение)
    i = bisect_right(ends, s)
    return i < len(starts) and starts[i] < e


def _validate_doctor(d: Doctor) -> None:
    if d.slot_minutes <= 0:
        raise ValueError(f"Врач {d.id}: slot_minutes должен быть > 0")
    if not d.work_start < d.work_end:
        raise ValueError(f"Врач {d.id}: work_start должен быть раньше work_end")
    for b in d.breaks:
        if not b[0] < b[1]:
            raise ValueError(f"Врач {d.id}: перерыв {b!r} имеет конец не позже начала")


# ---------------------------------------------------------------- основная функция

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
    # --- валидация входа
    if not isinstance(now, datetime) or not _is_aware(now):
        raise ValueError("now должен быть aware datetime")
    if not isinstance(duration_slots, int) or not 1 <= duration_slots <= 4:
        raise ValueError(f"duration_slots должен быть в диапазоне 1..4, получено {duration_slots!r}")
    if not isinstance(limit, int) or limit < 1:
        raise ValueError(f"limit должен быть >= 1, получено {limit!r}")
    if not isinstance(horizon_days, int) or horizon_days < 1:
        raise ValueError(f"horizon_days должен быть >= 1, получено {horizon_days!r}")

    # все интервалы проверяем независимо от специальности
    busy_merged: dict[int, tuple[list, list]] = {}
    for doc_id, intervals in busy.items():
        busy_merged[doc_id] = _merge(_check_intervals(intervals, f"busy[{doc_id}]"))
    patient_merged = _merge(_check_intervals(patient_busy, "patient_busy"))

    wanted = specialty.strip().casefold()
    matching = [d for d in doctors if d.specialty.strip().casefold() == wanted]
    for d in matching:
        _validate_doctor(d)

    # --- границы поиска
    now_utc = now.astimezone(UTC)
    horizon_end = now_utc + timedelta(days=horizon_days)
    # местные даты с запасом в сутки с каждой стороны; точный отбор по UTC-границам
    first_day = now_utc.astimezone(tz).date() - timedelta(days=1)
    last_day = horizon_end.astimezone(tz).date() + timedelta(days=1)
    days = []
    cur = first_day
    while cur <= last_day:
        days.append(cur)
        cur += timedelta(days=1)

    empty = ([], [])
    candidates: list[Option] = []

    for doc in matching:
        step = timedelta(minutes=doc.slot_minutes)
        length = step * duration_slots
        doc_busy = busy_merged.get(doc.id, empty)
        found = 0

        for day in days:
            if found >= limit:
                break
            if day in doc.days_off or day.isoweekday() not in doc.work_days:
                continue

            shift_end = datetime.combine(day, doc.work_end)      # «настенное» время
            wall = datetime.combine(day, doc.work_start)
            breaks = [(datetime.combine(day, bs), datetime.combine(day, be))
                      for bs, be in doc.breaks]

            while wall + length <= shift_end:
                wall_end = wall + length
                cur_wall = wall
                wall = wall + step                                # следующий узел сетки

                # перерывы (местное время, полуоткрытые)
                if any(cur_wall < be and wall_end > bs for bs, be in breaks):
                    continue

                # перевод в UTC; fold=0 -> первое вхождение повторяющегося времени
                start_utc = cur_wall.replace(tzinfo=tz).astimezone(UTC)
                # несуществующее время (пропуск при переходе на летнее) не переживёт круг
                if start_utc.astimezone(tz).replace(tzinfo=None) != cur_wall:
                    continue

                if start_utc <= now_utc:
                    continue
                if start_utc > horizon_end:
                    break                                         # дальше по дню только позже
                end_utc = start_utc + length

                if _overlaps(doc_busy, start_utc, end_utc):
                    continue
                if _overlaps(patient_merged, start_utc, end_utc):
                    continue

                candidates.append(Option(doc.id, start_utc, end_utc))
                found += 1
                if found >= limit:
                    break

    candidates.sort(key=lambda o: (o.starts_at, o.doctor_id))
    return candidates[:limit]

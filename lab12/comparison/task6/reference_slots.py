"""Эталонное решение задачи C1 (написано вручную для проверки скрытых тестов).

Модели его не видят. Нужно, чтобы убедиться, что тесты в test_find_slots.py
соответствуют спецификации: эталон обязан проходить их все.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Doctor:
    id: int
    specialty: str
    work_days: frozenset[int]
    work_start: time
    work_end: time
    slot_minutes: int
    breaks: tuple[tuple[time, time], ...] = ()
    days_off: frozenset[date] = frozenset()


@dataclass(frozen=True)
class Option:
    doctor_id: int
    starts_at: datetime
    ends_at: datetime


def _aware(value: datetime, what: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{what}: naive datetime is not allowed")


def _intervals(raw: list[tuple[datetime, datetime]], what: str) -> tuple[list[datetime], list[datetime]]:
    """Проверяет интервалы и возвращает отсортированные по началу списки начал и концов в UTC."""
    pairs = []
    for start, end in raw:
        _aware(start, what)
        _aware(end, what)
        if end <= start:
            raise ValueError(f"{what}: interval end must be later than start")
        pairs.append((start.astimezone(UTC), end.astimezone(UTC)))
    pairs.sort()
    return [s for s, _ in pairs], [e for _, e in pairs]


def _overlaps(starts: list[datetime], ends: list[datetime], max_len: timedelta, a: datetime, b: datetime) -> bool:
    """Есть ли интервал, пересекающий [a, b). Кандидаты — начала в (a - max_len, b)."""
    i = bisect_left(starts, a - max_len)
    while i < len(starts) and starts[i] < b:
        if ends[i] > a:
            return True
        i += 1
    return False


def _to_utc(local: datetime, tz: ZoneInfo) -> datetime | None:
    """Местное «настенное» время -> UTC; None, если такого времени нет (переход на летнее)."""
    aware = local.replace(tzinfo=tz, fold=0)
    utc = aware.astimezone(UTC)
    if utc.astimezone(tz).replace(tzinfo=None) != local:
        return None
    return utc


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
    _aware(now, "now")
    if not 1 <= duration_slots <= 4:
        raise ValueError("duration_slots must be within 1..4")
    if limit < 1:
        raise ValueError("limit must be at least 1")
    if horizon_days < 1:
        raise ValueError("horizon_days must be at least 1")
    p_starts, p_ends = _intervals(patient_busy, "patient_busy")
    p_max = max((e - s for s, e in zip(p_starts, p_ends, strict=True)), default=timedelta(0))
    doctor_busy = {doctor_id: _intervals(items, f"busy[{doctor_id}]") for doctor_id, items in busy.items()}

    now_utc = now.astimezone(UTC)
    latest = now_utc + timedelta(days=horizon_days)
    wanted = specialty.strip().casefold()
    first_day = now_utc.astimezone(tz).date()
    last_day = latest.astimezone(tz).date()

    options: list[Option] = []
    for doctor in doctors:
        if doctor.specialty.strip().casefold() != wanted:
            continue
        d_starts, d_ends = doctor_busy.get(doctor.id, ([], []))
        d_max = max((e - s for s, e in zip(d_starts, d_ends, strict=True)), default=timedelta(0))
        step = timedelta(minutes=doctor.slot_minutes)
        length = step * duration_slots
        day = first_day
        while day <= last_day:
            if day.isoweekday() in doctor.work_days and day not in doctor.days_off:
                shift_end = datetime.combine(day, doctor.work_end)
                cursor = datetime.combine(day, doctor.work_start)
                while cursor + length <= shift_end:
                    local_start, local_end = cursor, cursor + length
                    cursor += step
                    if any(
                        local_start < datetime.combine(day, b_end) and datetime.combine(day, b_start) < local_end
                        for b_start, b_end in doctor.breaks
                    ):
                        continue
                    start, end = _to_utc(local_start, tz), _to_utc(local_end, tz)
                    if start is None or end is None or not now_utc < start <= latest:
                        continue
                    if _overlaps(d_starts, d_ends, d_max, start, end) or _overlaps(p_starts, p_ends, p_max, start, end):
                        continue
                    options.append(Option(doctor.id, start, end))
            day += timedelta(days=1)
    options.sort(key=lambda o: (o.starts_at, o.doctor_id))
    return options[:limit]

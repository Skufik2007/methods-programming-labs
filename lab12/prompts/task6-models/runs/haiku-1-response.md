# Модуль slots.py: подбор ближайших окон для записи к врачу

## Код модуля

```python
"""Подбор ближайших окон для записи к врачу в поликлинике.

Только стандартная библиотека, Python 3.12.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo

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


# ----------------------------------------------------------------- валидация

def _require_aware(value: object, name: str) -> None:
    if not isinstance(value, datetime):
        raise ValueError(f"{name}: ожидается datetime, получен {type(value).__name__}")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name}: naive datetime недопустим, нужен datetime с часовым поясом")


def _normalize_intervals(intervals, name: str) -> list[tuple[datetime, datetime]]:
    result: list[tuple[datetime, datetime]] = []
    for start, end in intervals:
        _require_aware(start, f"{name}: начало интервала")
        _require_aware(end, f"{name}: конец интервала")
        if end <= start:
            raise ValueError(
                f"{name}: конец интервала ({end}) не позже его начала ({start})"
            )
        result.append((start.astimezone(UTC), end.astimezone(UTC)))
    return result


def _validate_doctor(doc: Doctor) -> None:
    if isinstance(doc.slot_minutes, bool) or not isinstance(doc.slot_minutes, int) \
            or doc.slot_minutes <= 0:
        raise ValueError(f"врач {doc.id}: slot_minutes должен быть целым числом > 0")
    if not doc.work_start < doc.work_end:
        raise ValueError(f"врач {doc.id}: work_start должен быть раньше work_end")
    if not doc.work_days <= frozenset(range(1, 8)):
        raise ValueError(f"врач {doc.id}: work_days должны содержать числа 1..7")


def _normalize_specialty(value: str) -> str:
    return value.strip().casefold()


# ------------------------------------------------------------ интервалы и время

def _merge(intervals: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    """Объединяет пересекающиеся и касающиеся полуоткрытые интервалы."""
    merged: list[tuple[datetime, datetime]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged


def _overlaps(
    starts: list[datetime],
    blocked: list[tuple[datetime, datetime]],
    start: datetime,
    end: datetime,
) -> bool:
    """Проверяет пересечение [start, end) с отсортированным списком непересекающихся [s, e).

    starts — список начал blocked (для bisect). Касание границами не считается.
    """
    i = bisect.bisect_right(starts, start)
    if i > 0 and blocked[i - 1][1] > start:
        return True
    return i < len(blocked) and blocked[i][0] < end


def _local_to_utc(wall: datetime, tz: tzinfo) -> datetime | None:
    """Настенное время клиники -> UTC.

    Возвращает None, если такого настенного времени не существует (пропуск при
    переходе на летнее время). Неоднозначное время берётся с fold=0.
    """
    utc = wall.replace(tzinfo=tz, fold=0).astimezone(UTC)
    if utc.astimezone(tz).replace(tzinfo=None) != wall:
        return None
    return utc


def _hits_break(day: date, breaks, start: datetime, end: datetime) -> bool:
    """Пересечение настенного окна [start, end) с перерывами дня (полуоткрытые)."""
    for b_start, b_end in breaks:
        if b_start >= b_end:
            continue
        if start < datetime.combine(day, b_end) and datetime.combine(day, b_start) < end:
            return True
    return False


# ------------------------------------------------------------------ поиск

def _doctor_options(
    doc: Doctor,
    blocked: list[tuple[datetime, datetime]],
    tz: tzinfo,
    now_utc: datetime,
    horizon_end: datetime,
    first_day: date,
    last_day: date,
    duration_slots: int,
    limit: int,
) -> list[Option]:
    """Ближайшие до limit свободных окон одного врача, по возрастанию времени."""
    starts = [b[0] for b in blocked]
    step = timedelta(minutes=doc.slot_minutes)
    window = timedelta(minutes=duration_slots * doc.slot_minutes)
    out: list[Option] = []

    day = first_day
    while day <= last_day and len(out) < limit:
        if day.isoweekday() in doc.work_days and day not in doc.days_off:
            shift_end = datetime.combine(day, doc.work_end)
            wall = datetime.combine(day, doc.work_start)
            while wall + window <= shift_end and len(out) < limit:
                utc_start = _local_to_utc(wall, tz)
                if (
                    utc_start is not None
                    and now_utc < utc_start <= horizon_end
                    and not _hits_break(day, doc.breaks, wall, wall + window)
                ):
                    utc_end = utc_start + window
                    if not _overlaps(starts, blocked, utc_start, utc_end):
                        out.append(Option(doc.id, utc_start, utc_end))
                wall += step
        day += timedelta(days=1)
    return out


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
    """Возвращает не более limit ближайших свободных окон врачей указанной специальности."""
    # Проверка входных данных.
    _require_aware(now, "now")
    if not isinstance(tz, tzinfo):
        raise ValueError("tz: ожидается часовой пояс, например ZoneInfo('Europe/Berlin')")
    if isinstance(duration_slots, bool) or not isinstance(duration_slots, int) \
            or not 1 <= duration_slots <= 4:
        raise ValueError(f"duration_slots должен быть целым от 1 до 4, получено {duration_slots!r}")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError(f"limit должен быть целым >= 1, получено {limit!r}")
    if isinstance(horizon_days, bool) or not isinstance(horizon_days, int) or horizon_days < 1:
        raise ValueError(f"horizon_days должен быть целым >= 1, получено {horizon_days!r}")

    for doc in doctors:
        _validate_doctor(doc)
    busy_norm = {
        doc_id: _normalize_intervals(intervals, f"busy[{doc_id}]")
        for doc_id, intervals in busy.items()
    }
    patient = _normalize_intervals(patient_busy, "patient_busy")

    # Границы поиска в абсолютном времени и в локальных датах клиники.
    now_utc = now.astimezone(UTC)
    try:
        horizon_end = now_utc + timedelta(days=horizon_days)
    except OverflowError:
        horizon_end = datetime.max.replace(tzinfo=UTC)
    first_day = now_utc.astimezone(tz).date()
    try:
        last_day = horizon_end.astimezone(tz).date()
    except OverflowError:
        last_day = date.max

    wanted = _normalize_specialty(specialty)
    found: list[Option] = []
    for doc in doctors:
        if _normalize_specialty(doc.specialty) != wanted:
            continue
        blocked = _merge(busy_norm.get(doc.id, []) + patient)
        found.extend(
            _doctor_options(
                doc, blocked, tz, now_utc, horizon_end,
                first_day, last_day, duration_slots, limit,
            )
        )

    found.sort(key=lambda o: (o.starts_at, o.doctor_id))
    return found[:limit]
```

## Алгоритм

1. **Проверка входных данных.** Все `datetime` должны быть aware, интервалы — с концом позже начала, `duration_slots` в 1..4, `limit` и `horizon_days` не меньше 1. Ошибки поднимаются как `ValueError` с текстом, в котором указано место проблемы.
2. **Отбор врачей и занятость.** Специальности сравниваются через `strip().casefold()`. Для каждого подходящего врача его занятые интервалы объединяются с занятыми интервалами пациента, сортируются и склеиваются в непересекающийся список. Касающиеся интервалы склеиваются, что не меняет результат для полуоткрытых отрезков.
3. **Перебор дней.** Локальные даты от даты `now` в часовом поясе клиники до даты конца горизонта. День пропускается, если его день недели не входит в `work_days` или дата есть в `days_off`.
4. **Сетка внутри смены.** Начала окон идут с шагом `slot_minutes` от `work_start`. Окно длиной `duration_slots * slot_minutes` должно целиком поместиться до `work_end`. Проверки перерывов и смены выполняются по настенному времени клиники.
5. **Перевод во время UTC.** Настенное время переводится в UTC с `fold=0`. Если время не существует из-за перехода на летнее время, начало пропускается. Обратная конвертация сверяет результат с исходным временем. Из двух вхождений повторяющегося времени берётся первое.
6. **Фильтры по абсолютному времени.** Начало строго после `now` и не позже `now + horizon_days`. Конец окна — `начало + длительность` в UTC. Проверка занятости идёт бинарным поиском по `bisect` в отсортированном списке блокировок.
7. **Ранний выход.** Для каждого врача кандидаты перебираются по возрастанию времени, и перебор прекращается, как только набрано `limit` окон. Затем результаты всех врачей сортируются по `(starts_at, doctor_id)` и обрезаются до `limit`.

## Сложность

Пусть `M` — число подходящих врачей, `H` — горизонт в днях, `S` — число слотов в смене, `B` — число занятых интервалов на врача.

- **Время:** `O(M * (B log B + H * S * log B))` в худшем случае. Слагаемое `B log B` — сортировка блокировок врача. Слагаемое `H * S * log B` — кандидаты на сетке и бинарный поиск по каждому. Ранний выход по `limit` часто сильно сокращает перебор.
- **Память:** `O(M * B + limit)`.
- **Оценка для 50 врачей, 14 дней, по 200 интервалов:** при смене 9 часов и шаге 15 минут на врача приходится около 540 кандидатов, то есть до 27 000 в худшем случае. Каждая проверка занимает считанные микросекунды, поэтому укладываться в 1 секунду должно с запасом. Это расчётная оценка: код не запускался.

## Допущения

- Длительность окна считается как абсолютный промежуток времени. В сутках с переходом на летнее время окно может по настенным часам выглядеть короче или длиннее.
- Перерывы и смена сравниваются по настенному времени клиники.
- Если `work_start` или `work_end` приходятся на несуществующее настенное время, соответствующее окно пропускается.
- Дополнительно к перечисленным в ТЗ проверкам валидируются `slot_minutes`, порядок `work_start` и `work_end` и значения `work_days`.

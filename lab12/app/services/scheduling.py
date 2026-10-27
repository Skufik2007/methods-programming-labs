"""Чистая логика расписания врача: смена, сетка слотов, пересечения, свободные слоты.

Модуль не знает ни про БД, ни про FastAPI и не читает системные часы: «текущий момент»
всегда приходит параметром ``now`` (это упрощает тесты).

Соглашения:

* все входные ``datetime`` - timezone-aware (в системе это UTC); naive-значения
  отвергаются с ``ValueError``;
* ``work_start`` / ``work_end`` / ``work_days`` - местное («настенное») время клиники;
  в местное время моменты переводятся через ``zoneinfo`` (параметр ``tz``);
* интервалы полуоткрытые ``[starts_at, ends_at)``: запись до 10:00 не пересекается
  с записью с 10:00;
* арифметика сетки ведётся по «настенным» часам. Для часовых поясов с переводом часов
  в рабочее время слоты в «пропавшем» (или повторяющемся) часе не поддерживаются;
  для Europe/Moscow (по умолчанию) перевода часов нет;
* правила нарушаются исключением ``SchedulingError`` с текстом для клиента; сервисный
  слой превращает его в ``BusinessRuleError``.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

MAX_BOOKING_DAYS_AHEAD = 90  # П1: запись не дальше чем на столько дней вперёд
MAX_SLOTS_PER_APPOINTMENT = 4  # П2: длительность записи - от 1 до 4 слотов


class SchedulingError(ValueError):
    """Нарушено правило расписания. Текст сообщения предназначен для показа клиенту."""


@dataclass(frozen=True, slots=True)
class Schedule:
    """График врача: рабочие дни (ISO, 1 = понедельник), смена и шаг сетки; время - местное."""

    work_days: frozenset[int]
    work_start: time
    work_end: time
    slot_minutes: int

    def __post_init__(self) -> None:
        if self.slot_minutes <= 0:
            raise ValueError("slot_minutes must be positive")
        if self.work_start >= self.work_end:
            raise ValueError("work_start must be earlier than work_end")
        if not self.work_days <= frozenset(range(1, 8)):
            raise ValueError("work_days must contain ISO weekday numbers 1..7")


@dataclass(frozen=True, slots=True)
class Slot:
    """Свободный слот приёма: полуоткрытый интервал ``[starts_at, ends_at)``, aware UTC."""

    starts_at: datetime
    ends_at: datetime


def _require_aware(*values: datetime) -> None:
    for value in values:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("naive datetime is not allowed: use timezone-aware datetime")


def _to_local_naive(value: datetime, tz: ZoneInfo) -> datetime:
    """Момент -> местное «настенное» время без tzinfo (для арифметики сетки)."""
    return value.astimezone(tz).replace(tzinfo=None)


def _local_to_utc(local_naive: datetime, tz: ZoneInfo) -> datetime:
    """Местное «настенное» время -> aware UTC."""
    return local_naive.replace(tzinfo=tz).astimezone(UTC)


def validate_booking_window(
    starts_at: datetime, *, now: datetime, max_days_ahead: int = MAX_BOOKING_DAYS_AHEAD
) -> None:
    """П1: запись только в будущее (строго после ``now``) и не дальше ``max_days_ahead`` дней вперёд.

    Верхняя граница включительна: начало ровно через 90 суток допустимо.
    """
    _require_aware(starts_at, now)
    if starts_at <= now:
        raise SchedulingError("Appointment must start in the future")
    if starts_at > now + timedelta(days=max_days_ahead):
        raise SchedulingError(f"Appointments can be booked at most {max_days_ahead} days in advance")


def validate_within_shift(starts_at: datetime, ends_at: datetime, schedule: Schedule, tz: ZoneInfo) -> None:
    """П2 (часть 1): интервал целиком лежит в одном рабочем дне и в смене врача.

    Проверяются (по местному времени клиники): интервал не пуст, не переходит через
    полночь, день недели рабочий, ``work_start <= начало`` и ``конец <= work_end``.
    """
    _require_aware(starts_at, ends_at)
    if ends_at <= starts_at:
        raise SchedulingError("ends_at must be later than starts_at")
    start = _to_local_naive(starts_at, tz)
    end = _to_local_naive(ends_at, tz)
    if start.date() != end.date():
        raise SchedulingError("Appointment must not cross midnight (clinic local time)")
    if start.isoweekday() not in schedule.work_days:
        raise SchedulingError("The doctor does not work on this day of the week")
    if start.time() < schedule.work_start or end.time() > schedule.work_end:
        raise SchedulingError(
            f"Appointment must fit into the doctor's shift {schedule.work_start:%H:%M}-"
            f"{schedule.work_end:%H:%M} (clinic local time)"
        )


def validate_slot_grid(starts_at: datetime, ends_at: datetime, schedule: Schedule, tz: ZoneInfo) -> None:
    """П2 (часть 2): сетка слотов.

    * начало совпадает с началом слота: ``work_start + k * slot_minutes``;
    * длительность - целое число слотов, от 1 до ``MAX_SLOTS_PER_APPOINTMENT``.
    """
    _require_aware(starts_at, ends_at)
    start = _to_local_naive(starts_at, tz)
    end = _to_local_naive(ends_at, tz)
    step = timedelta(minutes=schedule.slot_minutes)

    offset = start - datetime.combine(start.date(), schedule.work_start)
    if offset < timedelta(0) or offset % step != timedelta(0):
        raise SchedulingError(
            f"Appointment must start at a slot boundary: every {schedule.slot_minutes} minutes "
            f"from {schedule.work_start:%H:%M} (clinic local time)"
        )

    slots, remainder = divmod(end - start, step)
    if remainder != timedelta(0) or not 1 <= slots <= MAX_SLOTS_PER_APPOINTMENT:
        raise SchedulingError(
            f"Appointment duration must be from 1 to {MAX_SLOTS_PER_APPOINTMENT} slots "
            f"of {schedule.slot_minutes} minutes"
        )


def intervals_overlap(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    """Пересекаются ли полуоткрытые интервалы ``[a_start, a_end)`` и ``[b_start, b_end)``.

    Интервалы, касающиеся границами (``a_end == b_start``), не пересекаются.

    Raises:
        ValueError: один из интервалов пуст или перевёрнут.
    """
    if a_end <= a_start or b_end <= b_start:
        raise ValueError("interval must have positive length")
    return a_start < b_end and b_start < a_end


def local_day_bounds(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """Границы местных суток ``[00:00 дня, 00:00 следующего дня)`` как aware UTC.

    ``day`` должен быть не слишком близок к ``date.min`` / ``date.max`` (иначе ``OverflowError``);
    роуты ограничивают допустимые даты.
    """
    start = _local_to_utc(datetime.combine(day, time.min), tz)
    end = _local_to_utc(datetime.combine(day + timedelta(days=1), time.min), tz)
    return start, end


def free_slots(
    schedule: Schedule,
    day: date,
    tz: ZoneInfo,
    busy: Iterable[tuple[datetime, datetime]],
    *,
    now: datetime,
    max_days_ahead: int = MAX_BOOKING_DAYS_AHEAD,
) -> list[Slot]:
    """Свободные слоты врача на местную дату ``day``.

    Слот предлагается, если одновременно:

    * ``day`` - рабочий день врача, слот целиком внутри смены (последний неполный слот
      отбрасывается);
    * слот начинается строго после ``now`` (прошедшие и идущие слоты не предлагаются);
    * слот начинается не позже ``now + max_days_ahead`` суток (то же окно, что в ``validate_booking_window``);
    * слот не пересекается ни с одним интервалом из ``busy`` (занятые интервалы - неотменённые
      записи врача; интервал может занимать несколько слотов).

    Слоты возвращаются по возрастанию времени, в UTC.
    """
    _require_aware(now)
    if day.isoweekday() not in schedule.work_days:
        return []

    busy_intervals = tuple(busy)
    step = timedelta(minutes=schedule.slot_minutes)
    latest_start = now + timedelta(days=max_days_ahead)
    shift_end = datetime.combine(day, schedule.work_end)
    cursor = datetime.combine(day, schedule.work_start)

    result: list[Slot] = []
    while cursor + step <= shift_end:
        starts_at = _local_to_utc(cursor, tz)
        ends_at = _local_to_utc(cursor + step, tz)
        cursor += step
        if starts_at <= now or starts_at > latest_start:
            continue
        if any(intervals_overlap(starts_at, ends_at, b_start, b_end) for b_start, b_end in busy_intervals):
            continue
        result.append(Slot(starts_at=starts_at, ends_at=ends_at))
    return result

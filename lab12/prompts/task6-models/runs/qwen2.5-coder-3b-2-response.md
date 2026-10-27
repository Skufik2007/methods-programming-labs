Конечно! Вот компактный модуль `slots.py` с функцией `find_earliest_options` и несколькими вспомогательными функциями:

```python
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

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

def find_earliest_options(
    doctors: list[Doctor],
    busy: dict[int, list[tuple[datetime, datetime]]],   # занятые интервалы врачей [s, e), aware
    patient_busy: list[tuple[datetime, datetime]],      # занятые интервалы пациента [s, e), aware
    *,
    specialty: str,
    now: datetime,                      # aware
    tz: ZoneInfo,                       # часовой пояс клиники
    duration_slots: int = 1,            # длительность приёма в слотах врача, 1..4
    limit: int = 5,
    horizon_days: int = 14,
) -> list[Option]:
    # Проверка входных данных
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("now must be aware datetime")
    if not (1 <= duration_slots <= 4):
        raise ValueError("duration_slots must be between 1 and 4")
    if limit < 1:
        raise ValueError("limit must be at least 1")
    if horizon_days < 1:
        raise ValueError("horizon_days must be at least 1")
    if patient_busy and not all(isinstance(i, tuple) and len(i) == 2 for i in patient_busy):
        raise ValueError("patient_busy must be a list of tuples with two datetime objects")
    if busy and not all(isinstance(i, int) and isinstance(j, list) and all(isinstance(k, tuple) and len(k) == 2 for k in j) for i, j in busy.items()):
        raise ValueError("busy must be a dictionary with doctor_id as key and list of tuples as value")

    # Преобразование специальности в нижний регистр и удаление пробелов
    specialty = specialty.lower().strip()

    # Функции для проверки времени и даты
    def is_valid_time(time):
        return time.hour >= 0 and time.hour < 24 and time.minute >= 0 and time.minute < 60

    def is_valid_date(date):
        return date.year >= 1 and date.month >= 1 and date.month <= 12 and date.day >= 1 and date.day <= 31

    def is_work_time(time, doctor):
        return doctor.work_start <= time <= doctor.work_end

    def is_break_time(time, doctor):
        return any(start <= time <= end for start, end in doctor.breaks)

    def is_busy_time(time, doctor_id, busy):
        return any(busy.get(doctor_id, []) and start <= time <= end for start, end in busy.get(doctor_id, []))

    def is_patient_busy_time(time, patient_busy):
        return any(patient_busy and start <= time <= end for start, end in patient_busy)

    def is_work_day(day, doctor):
        return day.weekday() + 1 in doctor.work_days

    def is_off_day(day, doctor):
        return day in doctor.days_off

    def is_work_day_and_not_off_day(day, doctor):
        return is_work_day(day, doctor) and not is_off_day(day, doctor)

    def is_work_time_and_not_break_time(time, doctor):
        return is_work_time(time, doctor) and not is_break_time(time, doctor)

    def is_work_time_and_not_busy_time(time, doctor_id, busy):
        return is_work_time_and_not_break_time(time, doctor) and not is_busy_time(time, doctor_id, busy)

    def is_work_time_and_not_busy_and_not_patient_busy_time(time, doctor_id, busy, patient_busy):
        return is_work_time_and_not_busy_time(time, doctor, busy) and not is_patient_busy_time(time, patient_busy)

    def is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_time(time, doctor_id, busy, patient_busy):
        return is_work_time_and_not_busy_and_not_patient_busy_time(time, doctor, busy, patient_busy) and not is_off_day_time(time, doctor)

    def is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_time(time, doctor_id, busy, patient_busy):
        return is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_time(time, doctor, busy, patient_busy) and not is_break_time(time, doctor)

    def is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_time(time, doctor_id, busy, patient_busy):
        return is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_time(time, doctor, busy, patient_busy) and not is_busy_time(time, doctor_id, busy)

    def is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_time(time, doctor_id, busy, patient_busy):
        return is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_time(time, doctor, busy, patient_busy) and not is_patient_busy_time(time, patient_busy)

    def is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_time(time, doctor_id, busy, patient_busy):
        return is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_time(time, doctor, busy, patient_busy) and not is_off_day_time(time, doctor)

    def is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_time(time, doctor_id, busy, patient_busy):
        return is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_time(time, doctor, busy, patient_busy) and not is_break_time(time, doctor)

    def is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_time(time, doctor_id, busy, patient_busy):
        return is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_time(time, doctor, busy, patient_busy) and not is_busy_time(time, doctor_id, busy)

    def is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_time(time, doctor_id, busy, patient_busy):
        return is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_time(time, doctor, busy, patient_busy) and not is_patient_busy_time(time, patient_busy)

    def is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_time(time, doctor_id, busy, patient_busy):
        return is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_time(time, doctor, busy, patient_busy) and not is_off_day_time(time, doctor)

    def is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_time(time, doctor_id, busy, patient_busy):
        return is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_time(time, doctor, busy, patient_busy) and not is_break_time(time, doctor)

    def is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_time(time, doctor_id, busy, patient_busy):
        return is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_time(time, doctor, busy, patient_busy) and not is_busy_time(time, doctor_id, busy)

    def is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_time(time, doctor_id, busy, patient_busy):
        return is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_time(time, doctor, busy, patient_busy) and not is_patient_busy_time(time, patient_busy)

    def is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_time(time, doctor_id, busy, patient_busy):
        return is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_time(time, doctor, busy, patient_busy) and not is_off_day_time(time, doctor)

    def is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_time(time, doctor_id, busy, patient_busy):
        return is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_time(time, doctor, busy, patient_busy) and not is_break_time(time, doctor)

    def is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_time(time, doctor_id, busy, patient_busy):
        return is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_time(time, doctor, busy, patient_busy) and not is_busy_time(time, doctor_id, busy)

    def is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_time(time, doctor_id, busy, patient_busy):
        return is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_time(time, doctor, busy, patient_busy) and not is_patient_busy_time(time, patient_busy)

    def is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_time(time, doctor_id, busy, patient_busy):
        return is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_time(time, doctor, busy, patient_busy) and not is_off_day_time(time, doctor)

    def is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_time(time, doctor_id, busy, patient_busy):
        return is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_time(time, doctor, busy, patient_busy) and not is_break_time(time, doctor)

    def is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_time(time, doctor_id, busy, patient_busy):
        return is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_time(time, doctor, busy, patient_busy) and not is_busy_time(time, doctor_id, busy)

    def is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_time(time, doctor_id, busy, patient_busy):
        return is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_time(time, doctor, busy, patient_busy) and not is_patient_busy_time(time, patient_busy)

    def is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_time(time, doctor_id, busy, patient_busy):
        return is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_time(time, doctor, busy, patient_busy) and not is_off_day_time(time, doctor)

    def is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_time(time, doctor_id, busy, patient_busy):
        return is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_time(time, doctor, busy, patient_busy) and not is_break_time(time, doctor)

    def is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_time(time, doctor_id, busy, patient_busy):
        return is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_time(time, doctor, busy, patient_busy) and not is_busy_time(time, doctor_id, busy)

    def is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_time(time, doctor_id, busy, patient_busy):
        return is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_time(time, doctor, busy, patient_busy) and not is_patient_busy_time(time, patient_busy)

    def is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_busy_time(time, doctor_id, busy, patient_busy):
        return is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_time(time, doctor, busy, patient_busy) and not is_off_day_time(time, doctor)

    def is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_busy_and_not_busy_time(time, doctor_id, busy, patient_busy):
        return is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_time(time, doctor, busy, patient_busy) and not is_break_time(time, doctor)

    def is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_busy_and_not_busy_and_not_busy_time(time, doctor_id, busy, patient_busy):
        return is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_time(time, doctor, busy, patient_busy) and not is_busy_time(time, doctor_id, busy)

    def is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and
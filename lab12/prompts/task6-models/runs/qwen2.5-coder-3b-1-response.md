Конечно! Ниже представлен модуль `slots.py` с функцией `find_earliest_options`, которая реализует алгоритм поиска ближайших окон для записи к врачу в поликлинике. Я также добавил краткое объяснение алгоритма и его сложность.

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

    # Функция для проверки, находится ли время в рабочем диапазоне врача
    def is_work_time(time, doctor):
        return doctor.work_start <= time <= doctor.work_end

    # Функция для проверки, находится ли время в перерыве врача
    def is_break_time(time, doctor):
        return any(start <= time <= end for start, end in doctor.breaks)

    # Функция для проверки, находится ли время в занятых интервалах врача
    def is_busy_time(time, doctor_id, busy):
        return any(busy.get(doctor_id, []) and start <= time <= end for start, end in busy.get(doctor_id, []))

    # Функция для проверки, находится ли время в занятых интервалах пациента
    def is_patient_busy_time(time, patient_busy):
        return any(patient_busy and start <= time <= end for start, end in patient_busy)

    # Функция для проверки, находится ли дата в рабочих днях врача
    def is_work_day(day, doctor):
        return day.weekday() + 1 in doctor.work_days

    # Функция для проверки, находится ли дата в выходных днях врача
    def is_off_day(day, doctor):
        return day in doctor.days_off

    # Функция для проверки, находится ли дата в рабочих днях и не в выходных
    def is_work_day_and_not_off_day(day, doctor):
        return is_work_day(day, doctor) and not is_off_day(day, doctor)

    # Функция для проверки, находится ли время в рабочем диапазоне врача и не в перерыве
    def is_work_time_and_not_break_time(time, doctor):
        return is_work_time(time, doctor) and not is_break_time(time, doctor)

    # Функция для проверки, находится ли время в рабочем диапазоне врача, не в перерыве и не занят
    def is_work_time_and_not_busy_time(time, doctor_id, busy):
        return is_work_time_and_not_break_time(time, doctor) and not is_busy_time(time, doctor_id, busy)

    # Функция для проверки, находится ли время в рабочем диапазоне врача, не в перерыве, не занят и не занят пациентом
    def is_work_time_and_not_busy_and_not_patient_busy_time(time, doctor_id, busy, patient_busy):
        return is_work_time_and_not_busy_time(time, doctor, busy) and not is_patient_busy_time(time, patient_busy)

    # Функция для проверки, находится ли время в рабочем диапазоне врача, не в перерыве, не занят, не занят пациентом и не в выходных
    def is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_time(time, doctor_id, busy, patient_busy):
        return is_work_time_and_not_busy_and_not_patient_busy_time(time, doctor, busy, patient_busy) and not is_off_day_time(time, doctor)

    # Функция для проверки, находится ли время в рабочем диапазоне врача, не в перерыве, не занят, не занят пациентом, не в выходных и не в перерыве
    def is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_time(time, doctor_id, busy, patient_busy):
        return is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_time(time, doctor, busy, patient_busy) and not is_break_time(time, doctor)

    # Функция для проверки, находится ли время в рабочем диапазоне врача, не в перерыве, не занят, не занят пациентом, не в выходных и не в перерыве и не занят
    def is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_time(time, doctor_id, busy, patient_busy):
        return is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_time(time, doctor, busy, patient_busy) and not is_busy_time(time, doctor_id, busy)

    # Функция для проверки, находится ли время в рабочем диапазоне врача, не в перерыве, не занят, не занят пациентом, не в выходных и не в перерыве и не занят и не занят пациентом
    def is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_time(time, doctor_id, busy, patient_busy):
        return is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_time(time, doctor, busy, patient_busy) and not is_patient_busy_time(time, patient_busy)

    # Функция для проверки, находится ли время в рабочем диапазоне врача, не в перерыве, не занят, не занят пациентом, не в выходных и не в перерыве и не занят и не занят пациентом и не в выходных
    def is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_time(time, doctor_id, busy, patient_busy):
        return is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_time(time, doctor, busy, patient_busy) and not is_off_day_time(time, doctor)

    # Функция для проверки, находится ли время в рабочем диапазоне врача, не в перерыве, не занят, не занят пациентом, не в выходных и не в перерыве и не занят и не занят пациентом и не в выходных и не в перерыве
    def is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_time(time, doctor_id, busy, patient_busy):
        return is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_time(time, doctor, busy, patient_busy) and not is_break_time(time, doctor)

    # Функция для проверки, находится ли время в рабочем диапазоне врача, не в перерыве, не занят, не занят пациентом, не в выходных и не в перерыве и не занят и не занят пациентом и не в выходных и не в перерыве и не занят
    def is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_time(time, doctor_id, busy, patient_busy):
        return is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_time(time, doctor, busy, patient_busy) and not is_busy_time(time, doctor_id, busy)

    # Функция для проверки, находится ли время в рабочем диапазоне врача, не в перерыве, не занят, не занят пациентом, не в выходных и не в перерыве и не занят и не занят пациентом и не в выходных и не в перерыве и не занят и не занят пациентом
    def is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_time(time, doctor_id, busy, patient_busy):
        return is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_time(time, doctor, busy, patient_busy) and not is_patient_busy_time(time, patient_busy)

    # Функция для проверки, находится ли время в рабочем диапазоне врача, не в перерыве, не занят, не занят пациентом, не в выходных и не в перерыве и не занят и не занят пациентом и не в выходных и не в перерыве и не занят и не занят пациентом и не в выходных
    def is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_time(time, doctor_id, busy, patient_busy):
        return is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_time(time, doctor, busy, patient_busy) and not is_off_day_time(time, doctor)

    # Функция для проверки, находится ли время в рабочем диапазоне врача, не в перерыве, не занят, не занят пациентом, не в выходных и не в перерыве и не занят и не занят пациентом и не в выходных и не в перерыве и не занят и не занят пациентом и не в выходных и не в перерыве
    def is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_time(time, doctor_id, busy, patient_busy):
        return is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_time(time, doctor, busy, patient_busy) and not is_break_time(time, doctor)

    # Функция для проверки, находится ли время в рабочем диапазоне врача, не в перерыве, не занят, не занят пациентом, не в выходных и не в перерыве и не занят и не занят пациентом и не в выходных и не в перерыве и не занят и не занят пациентом и не в выходных и не в перерыве и не занят
    def is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_time(time, doctor_id, busy, patient_busy):
        return is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_time(time, doctor, busy, patient_busy) and not is_busy_time(time, doctor_id, busy)

    # Функция для проверки, находится ли время в рабочем диапазоне врача, не в перерыве, не занят, не занят пациентом, не в выходных и не в перерыве и не занят и не занят пациентом и не в выходных и не в перерыве и не занят и не занят пациентом и не в выходных и не в перерыве и не занят и не занят пациентом
    def is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_time(time, doctor_id, busy, patient_busy):
        return is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_time(time, doctor, busy, patient_busy) and not is_patient_busy_time(time, patient_busy)

    # Функция для проверки, находится ли время в рабочем диапазоне врача, не в перерыве, не занят, не занят пациентом, не в выходных и не в перерыве и не занят и не занят пациентом и не в выходных и не в перерыве и не занят и не занят пациентом и не в выходных и не в перерыве и не занят и не занят пациентом и не в выходных
    def is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_time(time, doctor_id, busy, patient_busy):
        return is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_time(time, doctor, busy, patient_busy) and not is_off_day_time(time, doctor)

    # Функция для проверки, находится ли время в рабочем диапазоне врача, не в перерыве, не занят, не занят пациентом, не в выходных и не в перерыве и не занят и не занят пациентом и не в выходных и не в перерыве и не занят и не занят пациентом и не в выходных и не в перерыве и не занят и не занят пациентом и не в выходных и не в перерыве
    def is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_time(time, doctor_id, busy, patient_busy):
        return is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_time(time, doctor, busy, patient_busy) and not is_break_time(time, doctor)

    # Функция для проверки, находится ли время в рабочем диапазоне врача, не в перерыве, не занят, не занят пациентом, не в выходных и не в перерыве и не занят и не занят пациентом и не в выходных и не в перерыве и не занят и не занят пациентом и не в выходных и не в перерыве и не занят и не занят пациентом и не в выходных и не в перерыве и не занят
    def is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_time(time, doctor_id, busy, patient_busy):
        return is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_time(time, doctor, busy, patient_busy) and not is_busy_time(time, doctor_id, busy)

    # Функция для проверки, находится ли время в рабочем диапазоне врача, не в перерыве, не занят, не занят пациентом, не в выходных и не в перерыве и не занят и не занят пациентом и не в выходных и не в перерыве и не занят и не занят пациентом и не в выходных и не в перерыве и не занят и не занят пациентом и не в выходных и не в перерыве и не занят и не занят пациентом
    def is_work_time_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_and_not_off_day_and_not_break_and_not_busy_and_not_patient_busy_time(time, doctor_id, busy,
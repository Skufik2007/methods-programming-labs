Конечно! Ниже приведен модуль `slots.py` с функцией подбора ближайших окон для записи к врачу в поликлинике, а также описана его логика и сложность.

```python
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import zoneinfo

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
    
    if not (now.tzinfo and tz):
        raise ValueError("Both now and tz must be aware datetime objects.")
    if duration_slots < 1 or duration_slots > 4:
        raise ValueError("duration_slots must be between 1 and 4.")
    if limit < 1:
        raise ValueError("limit must be at least 1.")
    if horizon_days < 1:
        raise ValueError("horizon_days must be at least 1.")

    # Convert now to UTC
    now_utc = now.astimezone(tz)

    earliest_options = []

    for doctor in doctors:
        if not (doctor.specialty.lower() == specialty.lower()):
            continue

        work_start_local = tz.localize(doctor.work_start)
        work_end_local = tz.localize(doctor.work_end)

        # Calculate the start of the first possible slot
        next_work_day = now_utc.date()
        while True:
            if next_work_day in doctor.days_off or not (next_work_day.weekday() + 1) in doctor.work_days:
                next_work_day += timedelta(days=1)
                continue

            break_start_local = work_start_local.replace(hour=0, minute=0).replace(second=0)

            # Find the first slot that starts after now_utc
            for k in range((now_utc - break_start_local) // doctor.slot_minutes + 1):
                start_time = break_start_local + timedelta(minutes=k * doctor.slot_minutes)
                
                if not (start_time > now_utc and 
                        all(not busy[doctor.id].overlaps(start_time, start_time + duration_slots * doctor.slot_minutes)) and
                        all(not patient_busy.overlaps(start_time, start_time + duration_slots * doctor.slot_minutes))):
                    continue

                end_time = start_time + duration_slots * doctor.slot_minutes
                
                # Check for overlaps with breaks
                if any(break_start_local <= start_time < break_end or 
                       break_start_local <= end_time < break_end for _, break_end in doctor.breaks):
                    continue

                earliest_options.append(Option(doctor.id, start_time.astimezone(tz), end_time.astimezone(tz)))

            next_work_day += timedelta(days=1)

        # Sort and limit the results
        if len(earliest_options) > limit:
            earliest_options.sort(key=lambda x: (x.starts_at, x.doctor_id))
            return earliest_options[:limit]

    return []

# Example usage:
if __name__ == "__main__":
    doctors = [
        Doctor(id=1, specialty="Cardiology", work_days={1, 2, 3, 4, 5}, 
               work_start=time(9), work_end=time(17),
               slot_minutes=30,
               breaks=((time(12, 30), time(13)),)),
        Doctor(id=2, specialty="Neurology", work_days={1, 2, 3, 4, 5}, 
               work_start=time(8), work_end=time(16),
               slot_minutes=30,
               breaks=((time(9, 30), time(10)),)),
    ]

    busy = {
        1: [(datetime.now(tz).replace(hour=12, minute=0), datetime.now(tz).replace(hour=14, minute=0))],
        2: [(datetime.now(tz).replace(hour=9, minute=30), datetime.now(tz).replace(hour=15, minute=30))]
    }

    patient_busy = [
        (datetime.now(tz).replace(hour=8, minute=0), datetime.now(tz).replace(hour=12, minute=0)),
        (datetime.now(tz).replace(hour=9, minute=30), datetime.now(tz).replace(hour=15, minute=30))
    ]

    options = find_earliest_options(
        doctors,
        busy,
        patient_busy,
        specialty="Cardiology",
        now=datetime.now(),
        tz=tz
    )

    for option in options:
        print(option)
```

### Алгоритм и сложность:

1. **Фильтрация врачей**: Проходим по списку врачей, выбирая только те, у которых специальность совпадает с заданной.

2. **Определение рабочего дня**: Для каждого врача на каждом рабочем дне (учитывая выходные и перерывы), определяем первый возможный момент для начала приёма, который начинается после `now_utc`.

3. **Проверка доступности времени**:
   - Проверяем, что окно не пересекается с другими занятыми интервалами врачей.
   - Проверяем, что окно не пересекается с другими занятыми интервалами пациента.

4. **Сортировка и ограничение результатов**: Сортируем найденные окна по времени начала и врачу, затем отбрасываем多余的 результаты, если их количество превышает `limit`.

5. **Временная сложность**:
   - Фильтрация врачей: O(n), где n — число врачей.
   - Определение рабочего дня для каждого врача на каждом дне: O(m * d), где m — количество дней в горизонте, d — количество дней недели (7).
   - Проверка доступности времени для каждого окна: O(k + p), где k — число перерывов у врача, p — общее количество занятых интервалов врачей и пациента.
   - Сортировка и ограничение результатов: O(r log r), где r — количество найденных окон.

6. **Пространственная сложность**:
   - Основные переменные занимают константное место, а временные структуры данных (например, множества) могут занимать дополнительную память в зависимости от количества врачей и интервалов.

Этот код эффективен для поиска ближайших окон при условии ограничений на количество врачей и дней.
Напиши тесты pytest для функции ниже. Она считает доступные минуты врача по
графику за период дат (обе границы включительно). Тесты должны ловить ошибки в
реализации: граничные случаи, неполные недели, разные рабочие дни, ошибку на
неверном периоде. Импортируй функцию так: `from minutes import available_minutes`.

```python
from datetime import date, datetime, time

def available_minutes(work_days: frozenset[int], work_start: time, work_end: time,
                      date_from: date, date_to: date) -> int:
    """Длина смены в минутах × число дней периода, чей ISO-день недели (1 = пн) в work_days.

    Raises: ValueError, если date_from позже date_to.
    """
    if date_from > date_to:
        raise ValueError("date_from must not be later than date_to")
    shift = datetime.combine(date_from, work_end) - datetime.combine(date_from, work_start)
    shift_minutes = int(shift.total_seconds() // 60)
    full_weeks, rest = divmod((date_to - date_from).days + 1, 7)
    days = full_weeks * len(work_days)
    first = date_from.isoweekday()
    days += sum(1 for step in range(rest) if (first - 1 + step) % 7 + 1 in work_days)
    return days * shift_minutes
```

Дай только код тестов одним блоком ```python.

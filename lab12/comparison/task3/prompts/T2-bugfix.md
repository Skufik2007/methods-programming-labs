В системе записи к врачу есть функция проверки, свободно ли время. Найди все
ошибки в этом коде и предложи исправленную версию. Интервалы записей полуоткрытые:
[starts_at, ends_at), то есть запись 10:00–10:30 и запись 10:30–11:00 не конфликтуют.

```python
from datetime import datetime

def is_slot_free(busy: list[tuple[datetime, datetime]], start: datetime, end: datetime) -> bool:
    """True, если [start, end) не пересекается ни с одним занятым интервалом."""
    for b_start, b_end in busy:
        if start <= b_end and b_start <= end:
            return False
        return True
```

Ответ: список найденных ошибок (по одной строке на ошибку) и исправленный код
одним блоком ```python.

# C1 — задача для сравнения моделей: подбор ближайших окон для записи

Один и тот же текст отправляется каждой модели без изменений. Модели не видят
проекта и тестов. Правильность решения проверяет скрытый набор тестов
`comparison/task6/test_find_slots.py`, написанный по этой спецификации до получения
решений.

Итерации: решение из первого ответа прогоняется тестами. Если есть падения, модели
отправляется второй запрос — список упавших тестов с сообщениями (без кода тестов);
всего не больше 3 итераций. Число итераций до полного прохождения — одна из метрик.

## Текст промпта

```text
Напиши на Python 3.12 модуль slots.py с функцией подбора ближайших окон для записи
к врачу в поликлинике. Только стандартная библиотека.

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
) -> list[Option]: ...

Правила:
1. Подходят врачи с указанной специальностью (сравнение без учёта регистра и
   пробелов по краям).
2. Сетка: окно начинается в work_start + k * slot_minutes (местное время) и длится
   duration_slots * slot_minutes; окно целиком внутри смены [work_start, work_end).
3. День — рабочий (work_days) и не в days_off.
4. Окно не пересекается ни с одним перерывом врача, ни с одним занятым интервалом
   врача, ни с одним занятым интервалом пациента. Интервалы полуоткрытые: касание
   границами пересечением не считается.
5. Окно начинается строго после now; начало не позже now + horizon_days суток.
6. Результат: не больше limit окон, отсортированных по (starts_at, doctor_id), все
   времена — aware UTC. Пустой список, если подходящих окон нет.
7. Ошибки входных данных — ValueError с понятным сообщением: naive datetime (в now
   или в любом интервале), duration_slots вне 1..4, limit < 1, horizon_days < 1,
   интервал с концом не позже начала.
8. Часовой пояс с переходом на летнее время (например Europe/Berlin): «настенное»
   время, которого не существует, пропускается; повторяющееся — берётся один раз
   (первое вхождение, fold=0).
9. Производительность: 50 врачей, горизонт 14 дней, по 200 занятых интервалов у
   каждого — не дольше 1 секунды.

Дай полный код модуля и кратко объясни алгоритм и сложность.
```

"""Демо-данные для показа системы управления клиникой.

Запуск (из корня проекта; ``DATABASE_URL`` и ``JWT_SECRET`` заданы, миграции применены)::

    SEED_PASSWORD='<пароль демо-пользователей>' python -m scripts.seed            # bash / zsh
    $env:SEED_PASSWORD='<пароль>'; python -m scripts.seed                         # PowerShell
    python -m scripts.seed --now 2026-10-12T09:00:00+03:00                        # зафиксировать «сегодня»

В Docker (``docker compose up -d`` уже выполнен; пароль берётся из окружения хоста)::

    export SEED_PASSWORD='<пароль демо-пользователей>'
    docker compose run --rm --no-deps -e SEED_PASSWORD app python -m scripts.seed

Что создаётся: администратор, регистратор, 4 врача (разные специальности, графики и длина слота),
20 пациентов (у троих есть учётная запись), записи на приём за последние 30 дней
(completed / no_show / cancelled) и на 14 дней вперёд (scheduled), записи медкарты с кодами МКБ-10
и рецепты к части завершённых приёмов. Все персонажи вымышленные, назначения лекарств - только
иллюстрация и не являются медицинскими рекомендациями.

Свойства:

* идемпотентность: пользователи ищутся по email, пациенты - по номеру полиса, профили врачей - по
  пользователю; история записей создаётся только если у демо-врачей ещё нет ни одной записи, поэтому
  повторный запуск (в том числе в другой день) ничего не дублирует и не меняет;
* пароль всех демо-пользователей берётся из переменной окружения ``SEED_PASSWORD`` (значения по
  умолчанию нет); в конце печатаются логины, но не пароль;
* детерминированность: генератор случайных чисел инициализирован константой ``RNG_SEED``; при одном и
  том же «текущем моменте» (``--now``) на пустой базе набор данных одинаков (кроме хэшей паролей:
  у bcrypt случайная соль); даты записей отсчитываются от «сегодня», поэтому в другой день набор
  сдвигается вместе с календарём;
* все записи проходят проверки ``app.services.scheduling`` (рабочий день, смена, сетка слотов,
  окно бронирования) и не пересекаются ни у врача, ни у пациента (в том числе отменённые).
"""

import argparse
import asyncio
import os
import random
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import SessionFactory, engine
from app.models import (
    Appointment,
    AppointmentStatus,
    Doctor,
    MedicalRecord,
    Patient,
    Prescription,
    User,
    UserRole,
)
from app.schemas import PASSWORD_MAX_BYTES, PASSWORD_MIN_LENGTH
from app.security import hash_password_async
from app.services import scheduling

RNG_SEED = 42  # фиксированное зерно генератора случайных чисел (подобрано: нагрузка по дням недели ровная)
EMAIL_DOMAIN = "clinic.example"  # зарезервированный TLD (RFC 2606): адреса заведомо не настоящие

HISTORY_DAYS = 30  # записи за последние N дней
FUTURE_DAYS = 14  # и на N дней вперёд
FUTURE_MARGIN = timedelta(hours=1)  # будущая запись начинается не раньше чем через час после «сейчас»
APPOINTMENTS_PER_DAY_PAST = (1, 4)  # записей у врача в рабочий день: от и до включительно
APPOINTMENTS_PER_DAY_FUTURE = (1, 3)
DURATION_SLOTS = (1, 2, 3)  # длительность приёма в слотах и вероятности (максимум по правилам - 4)
DURATION_WEIGHTS = (0.75, 0.2, 0.05)
PAST_STATUSES = (AppointmentStatus.COMPLETED, AppointmentStatus.NO_SHOW, AppointmentStatus.CANCELLED)
PAST_STATUS_WEIGHTS = (0.72, 0.10, 0.18)
# Кто создал запись (created_by): регистратор / администратор / сам пациент. Пациенты с аккаунтом
# часто записываются сами, остальных записывает персонал.
BOOKED_BY_ROLES = ("registrar", "admin", "patient")
BOOKED_BY_WEIGHTS_WITH_ACCOUNT = (0.50, 0.05, 0.45)
BOOKED_BY_WEIGHTS_STAFF_ONLY = (0.88, 0.12, 0.0)
PRESCRIPTION_PROBABILITY = 0.6  # доля завершённых приёмов с диагнозом, по которому выписан рецепт

POLICY_BASE = 7_700_000_000_000_000  # номера полисов: 16 цифр, вычисляются из номера пациента
POLICY_STEP = 1_234_567_891


class SeedError(RuntimeError):
    """Данные в БД несовместимы с демо-набором (например, email занят пользователем другой роли)."""


# --------------------------------------------------------------------------- #
# Исходные данные
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class StaffSpec:
    """Сотрудник без профиля: администратор или регистратор."""

    email: str
    full_name: str
    role: UserRole


@dataclass(frozen=True, slots=True)
class DoctorSpec:
    """Врач и его график (время - местное время клиники, дни - ISO: 1 = понедельник)."""

    email: str
    full_name: str
    specialty: str
    cabinet: str
    slot_minutes: int
    work_start: time
    work_end: time
    work_days: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class PatientSpec:
    """Пациент; ``email`` задан, если у пациента есть учётная запись."""

    full_name: str
    birth_date: date
    email: str | None = None


ADMIN = StaffSpec(f"admin@{EMAIL_DOMAIN}", "Орлов Дмитрий Андреевич", UserRole.ADMIN)
REGISTRAR = StaffSpec(f"registrar@{EMAIL_DOMAIN}", "Петрова Ольга Николаевна", UserRole.REGISTRAR)

# Графики подобраны так, чтобы смена укладывалась в целое число слотов: 16 по 30 мин, 24 по 20 мин,
# 8 по 45 мин, 24 по 15 мин. У врачей разные рабочие дни, в том числе суббота.
DOCTORS: tuple[DoctorSpec, ...] = (
    DoctorSpec(
        email=f"dr.sokolova@{EMAIL_DOMAIN}",
        full_name="Соколова Елена Викторовна",
        specialty="Терапевт",
        cabinet="101",
        slot_minutes=30,
        work_start=time(9),
        work_end=time(17),
        work_days=(1, 2, 3, 4, 5),
    ),
    DoctorSpec(
        email=f"dr.mironov@{EMAIL_DOMAIN}",
        full_name="Миронов Андрей Павлович",
        specialty="Кардиолог",
        cabinet="214",
        slot_minutes=20,
        work_start=time(10),
        work_end=time(18),
        work_days=(1, 2, 4, 5),
    ),
    DoctorSpec(
        email=f"dr.lebedeva@{EMAIL_DOMAIN}",
        full_name="Лебедева Наталья Игоревна",
        specialty="Невролог",
        cabinet="305",
        slot_minutes=45,
        work_start=time(9),
        work_end=time(15),
        work_days=(2, 3, 4, 6),
    ),
    DoctorSpec(
        email=f"dr.kozlov@{EMAIL_DOMAIN}",
        full_name="Козлов Сергей Михайлович",
        specialty="Офтальмолог",
        cabinet="118",
        slot_minutes=15,
        work_start=time(8),
        work_end=time(14),
        work_days=(1, 3, 5, 6),
    ),
)

PATIENTS: tuple[PatientSpec, ...] = (
    PatientSpec("Иванов Павел Сергеевич", date(1985, 3, 14), f"ivanov.pavel@{EMAIL_DOMAIN}"),
    PatientSpec("Смирнова Анна Дмитриевна", date(1992, 7, 21), f"smirnova.anna@{EMAIL_DOMAIN}"),
    PatientSpec("Кузнецов Алексей Николаевич", date(1978, 11, 2), f"kuznetsov.alexey@{EMAIL_DOMAIN}"),
    PatientSpec("Попова Мария Александровна", date(2001, 1, 30)),
    PatientSpec("Васильев Игорь Петрович", date(1969, 5, 17)),
    PatientSpec("Петрова Татьяна Ивановна", date(1957, 9, 8)),
    PatientSpec("Соколов Дмитрий Андреевич", date(1990, 12, 25)),
    PatientSpec("Михайлова Екатерина Сергеевна", date(1996, 4, 12)),
    PatientSpec("Новиков Артём Владимирович", date(2004, 6, 19)),
    PatientSpec("Фёдорова Ольга Борисовна", date(1974, 2, 3)),
    PatientSpec("Морозов Владимир Игоревич", date(1963, 10, 29)),
    PatientSpec("Волкова Наталья Викторовна", date(1988, 8, 16)),
    PatientSpec("Алексеев Никита Олегович", date(1999, 3, 5)),
    PatientSpec("Лебедев Роман Анатольевич", date(1981, 7, 7)),
    PatientSpec("Семёнова Ирина Павловна", date(1950, 12, 11)),
    PatientSpec("Егоров Максим Денисович", date(1994, 9, 23)),
    PatientSpec("Павлова Юлия Андреевна", date(1979, 5, 28)),
    PatientSpec("Козлова Светлана Михайловна", date(1966, 1, 15)),
    PatientSpec("Степанов Георгий Валентинович", date(1955, 4, 9)),
    PatientSpec("Николаева Дарья Евгеньевна", date(2003, 11, 21)),
)


@dataclass(frozen=True, slots=True)
class RxSpec:
    """Вариант назначения: препарат, дозировка, указания."""

    medication: str
    dosage: str
    instructions: str


@dataclass(frozen=True, slots=True)
class DiagnosisSpec:
    """Диагноз по МКБ-10, текст заметки врача и возможные назначения (может не быть)."""

    code: str
    notes: str
    prescriptions: tuple[RxSpec, ...] = ()


@dataclass(frozen=True, slots=True)
class ClinicalProfile:
    """Типичные причины обращения и диагнозы специальности."""

    reasons: tuple[str, ...]
    diagnoses: tuple[DiagnosisSpec, ...]


CLINICAL: dict[str, ClinicalProfile] = {
    "Терапевт": ClinicalProfile(
        reasons=(
            "Плановый осмотр",
            "Кашель и повышенная температура",
            "Повышенное артериальное давление",
            "Боль в пояснице",
            "Справка для работы",
            "Повторный приём",
        ),
        diagnoses=(
            DiagnosisSpec(
                "J06.9",
                "ОРВИ лёгкого течения. Першение в горле, температура до 37,5. Режим, обильное питьё, наблюдение.",
                (RxSpec("Парацетамол", "500 мг", "По 1 таблетке при температуре выше 38, не более 3 раз в сутки"),),
            ),
            DiagnosisSpec(
                "J20.9",
                "Острый бронхит. Кашель с мокротой 4 дня, хрипов нет. Явка на повторный осмотр через неделю.",
                (RxSpec("Амброксол", "30 мг", "По 1 таблетке 3 раза в день после еды, 7 дней"),),
            ),
            DiagnosisSpec(
                "I10",
                "Эссенциальная гипертензия. АД 150/95, жалобы на головную боль. Назначен контроль давления дома.",
                (RxSpec("Лизиноприл", "10 мг", "По 1 таблетке утром, постоянно; измерять давление ежедневно"),),
            ),
            DiagnosisSpec(
                "K29.7",
                "Гастрит неуточнённый. Тяжесть в эпигастрии после еды. Диета, исключить острое и алкоголь.",
                (RxSpec("Омепразол", "20 мг", "По 1 капсуле утром за 30 минут до еды, 14 дней"),),
            ),
            DiagnosisSpec(
                "M54.5",
                "Боль внизу спины. Ограничение наклонов, неврологического дефицита нет. Ограничить нагрузки.",
                (RxSpec("Ибупрофен", "400 мг", "По 1 таблетке 2 раза в день после еды, до 5 дней"),),
            ),
            DiagnosisSpec(
                "E11.9",
                "Сахарный диабет 2 типа без осложнений. Глюкоза натощак 7,2. Направление на анализ HbA1c.",
                (RxSpec("Метформин", "850 мг", "По 1 таблетке 2 раза в день во время еды"),),
            ),
        ),
    ),
    "Кардиолог": ClinicalProfile(
        reasons=(
            "Боли в области сердца",
            "Контроль артериального давления",
            "Одышка при нагрузке",
            "Перебои в работе сердца",
            "Повторный приём",
            "Плановый осмотр",
        ),
        diagnoses=(
            DiagnosisSpec(
                "I10",
                "Гипертоническая болезнь, давление выше целевого. Терапия скорректирована, ЭКГ без острой патологии.",
                (RxSpec("Амлодипин", "5 мг", "По 1 таблетке 1 раз в день утром"),),
            ),
            DiagnosisSpec(
                "I20.8",
                "Стенокардия напряжения: приступы на лестнице, проходят в покое. Назначена нагрузочная проба.",
                (RxSpec("Бисопролол", "5 мг", "По 1 таблетке утром, пульс не ниже 55 в минуту"),),
            ),
            DiagnosisSpec(
                "I25.1",
                "Атеросклеротическая болезнь сердца. Липиды повышены: диета, контроль через 3 месяца.",
                (RxSpec("Аторвастатин", "20 мг", "По 1 таблетке вечером, постоянно"),),
            ),
            DiagnosisSpec(
                "I48.9",
                "Фибрилляция предсердий, пароксизмальная форма. Суточное мониторирование ЭКГ, наблюдение.",
            ),
            DiagnosisSpec(
                "I50.0",
                "Хроническая сердечная недостаточность I-II ФК. Отёков нет, одышка при нагрузке. Контроль веса.",
                (RxSpec("Эналаприл", "5 мг", "По 1 таблетке 2 раза в день"),),
            ),
        ),
    ),
    "Невролог": ClinicalProfile(
        reasons=(
            "Головные боли",
            "Боль в шее",
            "Нарушение сна",
            "Онемение в руке",
            "Повторный приём",
            "Плановый осмотр",
        ),
        diagnoses=(
            DiagnosisSpec(
                "G43.9",
                "Мигрень неуточнённая. Приступы 2-3 раза в месяц с тошнотой. Рекомендован дневник головной боли.",
                (
                    RxSpec(
                        "Суматриптан", "50 мг", "По 1 таблетке в начале приступа; повторно не ранее чем через 2 часа"
                    ),
                ),
            ),
            DiagnosisSpec(
                "G44.2",
                "Головная боль напряжения. Связана с работой за компьютером, очаговой симптоматики нет.",
                (RxSpec("Ибупрофен", "400 мг", "По 1 таблетке при боли, не более 3 раз в сутки"),),
            ),
            DiagnosisSpec(
                "M54.2",
                "Цервикалгия. Болезненность паравертебральных точек, ограничение поворотов. ЛФК, массаж.",
            ),
            DiagnosisSpec(
                "G47.0",
                "Расстройства засыпания. Режим сна, ограничение кофеина вечером; повторная консультация через месяц.",
                (RxSpec("Мелатонин", "3 мг", "По 1 таблетке за 30 минут до сна, 14 дней"),),
            ),
            DiagnosisSpec(
                "G56.0",
                "Синдром запястного канала. Онемение пальцев по ночам. Ортез на ночь, направление на ЭНМГ.",
            ),
        ),
    ),
    "Офтальмолог": ClinicalProfile(
        reasons=(
            "Снижение остроты зрения",
            "Покраснение и зуд глаз",
            "Подбор очков",
            "Сухость в глазах",
            "Профилактический осмотр",
            "Повторный приём",
        ),
        diagnoses=(
            DiagnosisSpec("H52.1", "Миопия слабой степени. Подобрана коррекция, рекомендован осмотр раз в год."),
            DiagnosisSpec(
                "H10.1",
                "Острый атопический конъюнктивит. Отёк и зуд век, выделения скудные. Исключить контакт с аллергеном.",
                (RxSpec("Олопатадин 0,1%", "1 капля", "По 1 капле в каждый глаз 2 раза в день, 10 дней"),),
            ),
            DiagnosisSpec(
                "H04.1",
                "Синдром сухого глаза. Работа за экраном, жжение к вечеру. Режим работы, увлажнение.",
                (RxSpec("Гипромеллоза 0,32%", "1-2 капли", "По 1-2 капли в каждый глаз до 4 раз в день"),),
            ),
            DiagnosisSpec("H25.9", "Старческая катаракта начальная. Острота зрения снижена незначительно, наблюдение."),
            DiagnosisSpec(
                "H40.9",
                "Подозрение на глаукому. Внутриглазное давление на верхней границе нормы, назначено обследование.",
                (RxSpec("Тимолол 0,5%", "1 капля", "По 1 капле в оба глаза утром и вечером"),),
            ),
            DiagnosisSpec("H52.4", "Пресбиопия. Подобраны очки для работы на близком расстоянии."),
        ),
    ),
}


# --------------------------------------------------------------------------- #
# Планирование записей (чистая логика, без БД)
# --------------------------------------------------------------------------- #

Interval = tuple[datetime, datetime]


@dataclass(frozen=True, slots=True)
class PlannedAppointment:
    """Запись, спланированная до обращения к БД: врач и пациент заданы индексами."""

    doctor_idx: int
    patient_idx: int
    starts_at: datetime
    ends_at: datetime
    status: AppointmentStatus
    booked_by: str  # "registrar" | "admin" | "patient"
    created_at: datetime


def schedule_of(doctor: Doctor) -> scheduling.Schedule:
    """График врача из его профиля в БД в виде ``scheduling.Schedule``."""
    return scheduling.Schedule(
        work_days=frozenset(doctor.work_days),
        work_start=doctor.work_start,
        work_end=doctor.work_end,
        slot_minutes=doctor.slot_minutes,
    )


def _is_free(busy: Sequence[Interval], starts_at: datetime, ends_at: datetime) -> bool:
    """Не пересекается ли ``[starts_at, ends_at)`` ни с одним занятым интервалом."""
    return not any(scheduling.intervals_overlap(starts_at, ends_at, b_start, b_end) for b_start, b_end in busy)


def _pick_run(free: Sequence[scheduling.Slot], *, now: datetime, rng: random.Random) -> Interval | None:
    """Случайный отрезок из 1-3 подряд идущих свободных слотов.

    Слот годится, если он целиком в прошлом (закончился не позже ``now``) или начинается позже
    ``now + FUTURE_MARGIN``: приёмов «прямо сейчас» в демо-данных нет. Подряд идущие слоты
    не могут лежать по разные стороны от ``now``: между ними был бы зазор.
    """

    def usable(slot: scheduling.Slot) -> bool:
        return slot.ends_at <= now or slot.starts_at > now + FUTURE_MARGIN

    candidates = [i for i, slot in enumerate(free) if usable(slot)]
    if not candidates:
        return None
    first = rng.choice(candidates)
    wanted = rng.choices(DURATION_SLOTS, weights=DURATION_WEIGHTS)[0]
    last = first
    while (
        last - first + 1 < wanted
        and last + 1 < len(free)
        and free[last + 1].starts_at == free[last].ends_at
        and usable(free[last + 1])
    ):
        last += 1
    return free[first].starts_at, free[last].ends_at


def plan_appointments(
    schedules: Sequence[scheduling.Schedule],
    patient_has_account: Sequence[bool],
    *,
    now: datetime,
    tz: ZoneInfo,
    rng: random.Random,
) -> list[PlannedAppointment]:
    """Планирует записи всех врачей на окно ``[сегодня - 30 дней, сегодня + 14 дней]``.

    Для каждого врача и рабочего дня выбирается несколько свободных отрезков сетки слотов
    (``scheduling.free_slots`` с «нулевой» точкой отсчёта в начале суток возвращает все не занятые
    слоты дня, в том числе прошедшие), подбирается пациент без пересекающихся записей, затем
    назначается статус: прошедшая запись - completed / no_show / cancelled, будущая - scheduled.
    Отменённые записи тоже занимают время: в демо-данных нет пересечений вообще.
    """
    today = now.astimezone(tz).date()
    patient_count = len(patient_has_account)
    doctor_busy: list[list[Interval]] = [[] for _ in schedules]
    patient_busy: list[list[Interval]] = [[] for _ in range(patient_count)]
    planned: list[PlannedAppointment] = []

    for doctor_idx, schedule in enumerate(schedules):
        for offset in range(-HISTORY_DAYS, FUTURE_DAYS + 1):
            day = today + timedelta(days=offset)
            if day.isoweekday() not in schedule.work_days:
                continue
            day_start, _ = scheduling.local_day_bounds(day, tz)
            low, high = APPOINTMENTS_PER_DAY_PAST if offset < 0 else APPOINTMENTS_PER_DAY_FUTURE
            for _ in range(rng.randint(low, high)):
                free = scheduling.free_slots(
                    schedule, day, tz, doctor_busy[doctor_idx], now=day_start - timedelta(seconds=1)
                )
                run = _pick_run(free, now=now, rng=rng)
                if run is None:
                    break
                starts_at, ends_at = run
                patients = [p for p in range(patient_count) if _is_free(patient_busy[p], starts_at, ends_at)]
                if not patients:
                    continue
                patient_idx = rng.choice(patients)

                if starts_at > now:
                    status = AppointmentStatus.SCHEDULED
                    created_at = now - timedelta(minutes=rng.randint(30, 5 * 24 * 60))  # записались недавно
                else:
                    status = rng.choices(PAST_STATUSES, weights=PAST_STATUS_WEIGHTS)[0]
                    created_at = starts_at - timedelta(hours=rng.randint(3, 10 * 24))  # записались заранее
                weights = (
                    BOOKED_BY_WEIGHTS_WITH_ACCOUNT if patient_has_account[patient_idx] else BOOKED_BY_WEIGHTS_STAFF_ONLY
                )
                booked_by = rng.choices(BOOKED_BY_ROLES, weights=weights)[0]

                doctor_busy[doctor_idx].append((starts_at, ends_at))
                patient_busy[patient_idx].append((starts_at, ends_at))
                planned.append(
                    PlannedAppointment(doctor_idx, patient_idx, starts_at, ends_at, status, booked_by, created_at)
                )
    verify_plan(planned, schedules, tz=tz)
    return planned


def verify_plan(
    planned: Sequence[PlannedAppointment], schedules: Sequence[scheduling.Schedule], *, tz: ZoneInfo
) -> None:
    """Независимая проверка плана теми же функциями ``scheduling``, что использует API записи.

    Raises:
        RuntimeError: запись нарушает правило расписания или пересекается с другой записью врача
            либо пациента (это ошибка самого скрипта, а не данных).
    """
    for item in planned:
        try:
            scheduling.validate_booking_window(item.starts_at, now=item.created_at)
            scheduling.validate_within_shift(item.starts_at, item.ends_at, schedules[item.doctor_idx], tz)
            scheduling.validate_slot_grid(item.starts_at, item.ends_at, schedules[item.doctor_idx], tz)
        except scheduling.SchedulingError as exc:
            raise RuntimeError(f"seed plan violates a scheduling rule: {exc}") from exc

    groups: dict[tuple[str, int], list[Interval]] = {}
    for item in planned:
        interval = (item.starts_at, item.ends_at)
        groups.setdefault(("doctor", item.doctor_idx), []).append(interval)
        groups.setdefault(("patient", item.patient_idx), []).append(interval)
    for (kind, owner), intervals in groups.items():
        intervals.sort()
        for (a_start, a_end), (b_start, b_end) in zip(intervals, intervals[1:], strict=False):
            if scheduling.intervals_overlap(a_start, a_end, b_start, b_end):
                raise RuntimeError(f"seed plan has overlapping appointments for {kind} #{owner}")


# --------------------------------------------------------------------------- #
# Запись в БД
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class SeedReport:
    """Итог запуска: что создано и какие логины есть в демо-наборе."""

    created: Counter[str] = field(default_factory=Counter)
    logins: list[tuple[str, str, str]] = field(default_factory=list)  # (роль, email, ФИО)
    history_skipped: bool = False


async def _get_or_create_user(
    session: AsyncSession, email: str, full_name: str, role: UserRole, password: str, report: SeedReport
) -> User:
    """Пользователь по email; если его нет - создаётся с bcrypt-хэшем пароля (существующий не меняется)."""
    user = await session.scalar(select(User).where(User.email == email))
    if user is None:
        user = User(
            email=email,
            password_hash=await hash_password_async(password),
            full_name=full_name,
            role=role,
            is_active=True,
        )
        session.add(user)
        report.created["users"] += 1
    elif user.role != role:
        raise SeedError(f"{email}: пользователь уже существует с ролью {user.role.value!r}, а нужна {role.value!r}")
    report.logins.append((role.value, email, user.full_name))
    return user


async def _ensure_doctors(session: AsyncSession, password: str, report: SeedReport) -> list[Doctor]:
    """Учётные записи и профили демо-врачей (в порядке ``DOCTORS``)."""
    doctors: list[Doctor] = []
    for spec in DOCTORS:
        user = await _get_or_create_user(session, spec.email, spec.full_name, UserRole.DOCTOR, password, report)
        await session.flush()
        doctor = await session.scalar(select(Doctor).where(Doctor.user_id == user.id))
        if doctor is None:
            doctor = Doctor(
                user_id=user.id,
                specialty=spec.specialty,
                cabinet=spec.cabinet,
                slot_minutes=spec.slot_minutes,
                work_start=spec.work_start,
                work_end=spec.work_end,
                work_days=list(spec.work_days),
            )
            session.add(doctor)
            report.created["doctors"] += 1
        doctors.append(doctor)
    await session.flush()
    return doctors


def policy_number(index: int) -> str:
    """Номер полиса ОМС (16 цифр) пациента с порядковым номером ``index`` (с нуля); одинаков при каждом запуске."""
    return f"{POLICY_BASE + (index + 1) * POLICY_STEP:016d}"


def phone_number(index: int) -> str:
    """Вымышленный телефон в формате E.164 для пациента с порядковым номером ``index``."""
    return f"+7900555{index + 1:04d}"


async def _ensure_patients(
    session: AsyncSession, password: str, report: SeedReport
) -> list[tuple[Patient, int | None]]:
    """Пациенты (поиск по номеру полиса) и аккаунты тех, у кого они заданы.

    Returns:
        Пары ``(пациент, id пользователя или None)`` в порядке ``PATIENTS``.
    """
    result: list[tuple[Patient, int | None]] = []
    for index, spec in enumerate(PATIENTS):
        user_id: int | None = None
        if spec.email is not None:
            user = await _get_or_create_user(session, spec.email, spec.full_name, UserRole.PATIENT, password, report)
            await session.flush()
            user_id = user.id
        patient = await session.scalar(select(Patient).where(Patient.policy_number == policy_number(index)))
        if patient is None:
            patient = Patient(
                user_id=user_id,
                full_name=spec.full_name,
                birth_date=spec.birth_date,
                phone=phone_number(index),
                policy_number=policy_number(index),
            )
            session.add(patient)
            report.created["patients"] += 1
        result.append((patient, user_id))
    await session.flush()
    return result


async def _create_history(
    session: AsyncSession,
    doctors: Sequence[Doctor],
    patients: Sequence[tuple[Patient, int | None]],
    staff_ids: dict[str, int],
    *,
    now: datetime,
    rng: random.Random,
    report: SeedReport,
) -> None:
    """Записи на приём, записи медкарты и рецепты. Не вызывается, если у демо-врачей уже есть записи."""
    tz = get_settings().clinic_tz
    planned = plan_appointments(
        [schedule_of(doctor) for doctor in doctors],
        [user_id is not None for _, user_id in patients],
        now=now,
        tz=tz,
        rng=rng,
    )

    appointments: list[Appointment] = []
    for item in planned:
        patient, patient_user_id = patients[item.patient_idx]
        creator = (
            patient_user_id
            if item.booked_by == "patient" and patient_user_id is not None
            else staff_ids[item.booked_by]
        )
        profile = CLINICAL[DOCTORS[item.doctor_idx].specialty]
        appointments.append(
            Appointment(
                doctor_id=doctors[item.doctor_idx].id,
                patient_id=patient.id,
                starts_at=item.starts_at,
                ends_at=item.ends_at,
                status=item.status,
                reason=rng.choice(profile.reasons),
                created_by=creator,
                created_at=item.created_at,
            )
        )
    session.add_all(appointments)
    await session.flush()
    report.created["appointments"] += len(appointments)

    for item, appointment in zip(planned, appointments, strict=True):
        if item.status != AppointmentStatus.COMPLETED:
            continue
        diagnosis = rng.choice(CLINICAL[DOCTORS[item.doctor_idx].specialty].diagnoses)
        recorded_at = min(item.ends_at + timedelta(minutes=rng.randint(5, 30)), now)
        session.add(
            MedicalRecord(
                patient_id=appointment.patient_id,
                doctor_id=appointment.doctor_id,
                appointment_id=appointment.id,
                diagnosis_code=diagnosis.code,
                notes=diagnosis.notes,
                created_at=recorded_at,
            )
        )
        report.created["medical_records"] += 1
        if diagnosis.prescriptions and rng.random() < PRESCRIPTION_PROBABILITY:
            rx = rng.choice(diagnosis.prescriptions)
            session.add(
                Prescription(
                    appointment_id=appointment.id,
                    medication=rx.medication,
                    dosage=rx.dosage,
                    instructions=rx.instructions,
                    issued_at=recorded_at,
                )
            )
            report.created["prescriptions"] += 1


async def seed(session: AsyncSession, password: str, *, now: datetime) -> SeedReport:
    """Создаёт демо-данные в одной транзакции (``commit`` - в вызывающем коде).

    Args:
        session: открытая сессия БД.
        password: пароль всех демо-пользователей.
        now: «текущий момент» (aware): от него отсчитываются 30 дней назад и 14 вперёд.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    report = SeedReport()
    rng = random.Random(RNG_SEED)  # noqa: S311 - детерминированные демо-данные, не криптография

    staff_ids: dict[str, int] = {}
    for key, spec in (("admin", ADMIN), ("registrar", REGISTRAR)):
        user = await _get_or_create_user(session, spec.email, spec.full_name, spec.role, password, report)
        await session.flush()
        staff_ids[key] = user.id
    doctors = await _ensure_doctors(session, password, report)
    patients = await _ensure_patients(session, password, report)

    existing = await session.scalar(
        select(func.count(Appointment.id)).where(Appointment.doctor_id.in_([doctor.id for doctor in doctors]))
    )
    if existing:
        report.history_skipped = True
    else:
        await _create_history(session, doctors, patients, staff_ids, now=now, rng=rng, report=report)
    return report


# --------------------------------------------------------------------------- #
# Командная строка
# --------------------------------------------------------------------------- #


def _aware_datetime(value: str) -> datetime:
    """Разбирает ``--now``: ISO 8601 со смещением, например ``2026-10-12T09:00:00+03:00``."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"не дата ISO 8601: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("укажите смещение часового пояса, например +03:00 или Z")
    return parsed.astimezone(UTC)


def _password_problem(password: str) -> str | None:
    """Почему пароль не годится (те же правила, что у API: длина и 72 байта bcrypt), или ``None``."""
    if not password:
        return "переменная окружения SEED_PASSWORD не задана"
    if len(password) < PASSWORD_MIN_LENGTH:
        return f"SEED_PASSWORD короче {PASSWORD_MIN_LENGTH} символов"
    if len(password.encode("utf-8")) > PASSWORD_MAX_BYTES:
        return f"SEED_PASSWORD длиннее {PASSWORD_MAX_BYTES} байт в UTF-8 (предел bcrypt)"
    return None


def _print_report(report: SeedReport) -> None:
    """Печатает итог и список логинов (пароль не печатается никогда)."""
    if report.created:
        summary = ", ".join(f"{name}: {count}" for name, count in report.created.items())
        print(f"Создано - {summary}.")
    else:
        print("Все демо-данные уже были в базе, ничего не изменено.")
    if report.history_skipped and report.created:
        print("История записей не создавалась: у демо-врачей уже есть записи.")
    print("\nЛогины демо-пользователей (пароль - значение SEED_PASSWORD):")
    width = max(len(email) for _, email, _ in report.logins)
    for role, email, full_name in report.logins:
        print(f"  {role:<10} {email:<{width}}  {full_name}")


async def _run(password: str, now: datetime) -> int:
    """Выполняет посев в одной транзакции и печатает результат; возвращает код завершения."""
    try:
        async with SessionFactory() as session:
            try:
                report = await seed(session, password, now=now)
                await session.commit()
            except SeedError as exc:
                await session.rollback()
                print(f"Ошибка: {exc}", file=sys.stderr)
                return 1
    finally:
        await engine.dispose()
    _print_report(report)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Точка входа ``python -m scripts.seed``; возвращает код завершения процесса."""
    parser = argparse.ArgumentParser(prog="python -m scripts.seed", description="Заполняет БД демо-данными.")
    parser.add_argument(
        "--now",
        type=_aware_datetime,
        default=None,
        help="«текущий момент» в ISO 8601 со смещением (по умолчанию - сейчас); нужен для воспроизводимости",
    )
    args = parser.parse_args(argv)

    password = os.environ.get("SEED_PASSWORD", "")
    problem = _password_problem(password)
    if problem is not None:
        print(f"Ошибка: {problem}. Пример: SEED_PASSWORD='...' python -m scripts.seed", file=sys.stderr)
        return 2
    return asyncio.run(_run(password, args.now or datetime.now(UTC)))


if __name__ == "__main__":
    sys.exit(main())

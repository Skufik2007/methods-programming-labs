# Аутентификация и записи на приём

Файлы `config.py`, `db.py`, `models.py`, `schemas.py`, `security.py` **не менялись**: всё нужное (в том числе схема `RegisterIn` и модель `SlotOut`) определено в новых файлах.

### app/__init__.py

```python
"""Система управления клиникой: FastAPI + SQLAlchemy 2.0 (async) + Pydantic v2."""
```

### app/services/__init__.py

```python
"""Бизнес-логика: расписание врача (чистые функции) и операции с записями (слой БД)."""
```

### app/routers/__init__.py

```python
"""HTTP-роуты (тонкий слой над сервисами)."""
```

### app/errors.py

```python
"""Доменные исключения и их преобразование в HTTP-ответы.

Сервисный слой бросает исключения, ничего не зная про HTTP; обработчик, зарегистрированный
функцией ``register_exception_handlers``, превращает их в ответ ``{"detail": "..."}``:

* ``NotFoundError``         - 404 (объект не найден или скрыт от текущего пользователя);
* ``ConflictError``         - 409 (конфликт с текущим состоянием: занятое время, неверный переход статуса);
* ``PermissionDeniedError`` - 403 (операция запрещена роли или владельцу);
* ``BusinessRuleError``     - 422 (нарушено бизнес-правило: прошлое время, вне смены, не по сетке).

Также здесь лежит ``is_unique_violation`` - классификация ``IntegrityError`` (PostgreSQL / SQLite).
"""

from typing import ClassVar

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError

_PG_UNIQUE_VIOLATION = "23505"
# SQLITE_CONSTRAINT_PRIMARYKEY, SQLITE_CONSTRAINT_UNIQUE
_SQLITE_UNIQUE_CODES: frozenset[int] = frozenset({1555, 2067})


class DomainError(Exception):
    """Базовое доменное исключение: ``detail`` - текст для клиента, ``status_code`` - HTTP-статус."""

    status_code: ClassVar[int] = 500

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class NotFoundError(DomainError):
    """Объект не найден (или скрыт от текущего пользователя - снаружи это неразличимо)."""

    status_code = 404


class ConflictError(DomainError):
    """Запрос конфликтует с текущим состоянием данных."""

    status_code = 409


class PermissionDeniedError(DomainError):
    """У пользователя нет права на операцию."""

    status_code = 403


class BusinessRuleError(DomainError):
    """Запрос синтаксически корректен, но нарушает бизнес-правило."""

    status_code = 422


def is_unique_violation(exc: IntegrityError) -> bool:
    """Возвращает ``True``, если ``IntegrityError`` вызван нарушением уникальности.

    Нужна, чтобы превращать в 409 только гонки за уникальный индекс, а не любые ошибки
    целостности (нарушение внешнего ключа или CHECK - это баг, а не конфликт клиента).

    * PostgreSQL (asyncpg): SQLSTATE ``23505`` (атрибут ``sqlstate`` / ``pgcode`` у ``exc.orig``);
    * SQLite: ``sqlite_errorcode`` 2067 / 1555 (Python 3.11+) либо текст ``UNIQUE constraint failed``.
    """
    for error in (exc.orig, getattr(exc.orig, "__cause__", None)):
        if error is None:
            continue
        if _PG_UNIQUE_VIOLATION in (getattr(error, "sqlstate", None), getattr(error, "pgcode", None)):
            return True
        if getattr(error, "sqlite_errorcode", None) in _SQLITE_UNIQUE_CODES:
            return True
    return "UNIQUE constraint failed" in str(exc.orig)


async def _handle_domain_error(_request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, DomainError):  # недостижимо: обработчик зарегистрирован только для DomainError
        raise exc
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


def register_exception_handlers(app: FastAPI) -> None:
    """Регистрирует обработчик доменных ошибок.

    Starlette ищет обработчик по MRO исключения, поэтому одного обработчика на базовый
    ``DomainError`` достаточно для всех четырёх подклассов.
    """
    app.add_exception_handler(DomainError, _handle_domain_error)
```

### app/services/scheduling.py

```python
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
from datetime import date, datetime, time, timedelta, timezone
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
    return local_naive.replace(tzinfo=tz).astimezone(timezone.utc)


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
```

### app/services/appointments.py

```python
"""Операции с записями на приём (слой БД): создание, чтение, список, перенос, смена статуса.

Слой владеет транзакцией: ``commit()`` вызывается здесь, роуты ничего не коммитят.
Права на уровне объектов («чья запись», окно в 2 часа для пациента) тоже проверяются
здесь; грубый фильтр по роли роуты делают через ``require_roles`` с теми же множествами
ролей (``BOOKING_ROLES``, ``STATUS_CHANGE_ROLES``), так что правила не расходятся.

Гонки (П6). Перед проверкой пересечений блокируется строка врача (``SELECT ... FOR UPDATE``),
затем строка пациента: две одновременные записи к одному врачу (или одного пациента к
разным врачам) выполняются по очереди, и вторая видит результат первой (PostgreSQL,
READ COMMITTED: каждый запрос видит данные, зафиксированные к его началу). Порядок
блокировок везде один - запись -> врач -> пациент, поэтому взаимных блокировок нет.
SQLite (тесты) ``FOR UPDATE`` игнорирует. Последний рубеж - частичный уникальный индекс
``(doctor_id, starts_at)``: его ``IntegrityError`` превращается в 409.

Видимость. Врач и пациент видят только свои записи; чужая запись неотличима от
несуществующей (404). Условие видимости входит в сам SQL-запрос, поэтому чужие строки
не читаются и не блокируются.

Время: все ``datetime`` - aware UTC; «сейчас» приходит параметром ``now``.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import ColumnElement, and_, false, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from app.config import get_settings
from app.errors import (
    BusinessRuleError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    is_unique_violation,
)
from app.models import Appointment, AppointmentStatus, Doctor, Patient, User, UserRole
from app.schemas import AppointmentCreate, AppointmentUpdate
from app.services import scheduling

STAFF_ROLES: frozenset[UserRole] = frozenset({UserRole.ADMIN, UserRole.REGISTRAR})
# Кто может создавать, переносить и отменять записи (пациент - только свои, см. ниже).
BOOKING_ROLES: frozenset[UserRole] = STAFF_ROLES | {UserRole.PATIENT}
# Кто может переводить запись в указанный статус.
STATUS_CHANGE_ROLES: dict[AppointmentStatus, frozenset[UserRole]] = {
    AppointmentStatus.CANCELLED: BOOKING_ROLES,
    AppointmentStatus.COMPLETED: frozenset({UserRole.DOCTOR}),
    AppointmentStatus.NO_SHOW: frozenset({UserRole.DOCTOR}),
}
# П7: разрешённые переходы статусов; из конечных статусов выхода нет.
ALLOWED_TRANSITIONS: dict[AppointmentStatus, frozenset[AppointmentStatus]] = {
    AppointmentStatus.SCHEDULED: frozenset(
        {AppointmentStatus.COMPLETED, AppointmentStatus.NO_SHOW, AppointmentStatus.CANCELLED}
    ),
    AppointmentStatus.COMPLETED: frozenset(),
    AppointmentStatus.NO_SHOW: frozenset(),
    AppointmentStatus.CANCELLED: frozenset(),
}
PATIENT_CHANGE_DEADLINE = timedelta(hours=2)  # пациент меняет/отменяет не позже чем за 2 часа до начала


@dataclass(frozen=True, slots=True)
class Actor:
    """Пользователь, выполняющий операцию, и его профили (врач / пациент)."""

    user: User
    doctor_id: int | None = None
    patient_id: int | None = None

    @property
    def role(self) -> UserRole:
        return self.user.role

    def visibility_clause(self) -> ColumnElement[bool] | None:
        """SQL-условие «какие записи пользователю видны»; ``None`` - ограничений нет.

        Врач без профиля ``Doctor`` и пациент без профиля ``Patient`` не видят ничего.
        """
        if self.role in STAFF_ROLES:
            return None
        if self.role == UserRole.DOCTOR and self.doctor_id is not None:
            return Appointment.doctor_id == self.doctor_id
        if self.role == UserRole.PATIENT and self.patient_id is not None:
            return Appointment.patient_id == self.patient_id
        return false()


@dataclass(frozen=True, slots=True)
class AppointmentFilters:
    """Фильтры списка записей. ``date_from`` / ``date_to`` - местные даты клиники, включительно."""

    doctor_id: int | None = None
    patient_id: int | None = None
    status: AppointmentStatus | None = None
    date_from: date | None = None
    date_to: date | None = None


async def load_actor(session: AsyncSession, user: User) -> Actor:
    """Определяет профиль врача / пациента пользователя (для остальных ролей профили не нужны)."""
    if user.role == UserRole.DOCTOR:
        doctor_id = await session.scalar(select(Doctor.id).where(Doctor.user_id == user.id))
        return Actor(user=user, doctor_id=doctor_id)
    if user.role == UserRole.PATIENT:
        patient_id = await session.scalar(select(Patient.id).where(Patient.user_id == user.id))
        return Actor(user=user, patient_id=patient_id)
    return Actor(user=user)


# --------------------------------------------------------------------------- #
# Внутренние помощники
# --------------------------------------------------------------------------- #


def _schedule_of(doctor: Doctor) -> scheduling.Schedule:
    return scheduling.Schedule(
        work_days=frozenset(doctor.work_days),
        work_start=doctor.work_start,
        work_end=doctor.work_end,
        slot_minutes=doctor.slot_minutes,
    )


async def _lock_doctor(session: AsyncSession, doctor_id: int) -> Doctor:
    """Читает врача и блокирует его строку до конца транзакции (П6)."""
    stmt = (
        select(Doctor)
        .where(Doctor.id == doctor_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    doctor = await session.scalar(stmt)
    if doctor is None:
        raise NotFoundError("Doctor not found")
    return doctor


async def _lock_patient(session: AsyncSession, patient_id: int) -> None:
    """Блокирует строку пациента (защита П4 от гонки записей к разным врачам)."""
    found = await session.scalar(select(Patient.id).where(Patient.id == patient_id).with_for_update())
    if found is None:
        raise NotFoundError("Patient not found")


async def _doctor_is_active(session: AsyncSession, doctor: Doctor) -> bool:
    """Активен ли аккаунт врача (``User.is_active``)."""
    return bool(await session.scalar(select(User.is_active).where(User.id == doctor.user_id)))


async def _has_overlap(
    session: AsyncSession,
    owner_column: InstrumentedAttribute[int],
    owner_id: int,
    starts_at: datetime,
    ends_at: datetime,
    exclude_id: int | None,
) -> bool:
    """Есть ли неотменённая запись владельца (врача или пациента), пересекающая ``[starts_at, ends_at)``.

    Условие ``starts_at < :end AND ends_at > :start`` - это ``scheduling.intervals_overlap``
    для полуоткрытых интервалов, выраженное в SQL (пересечение, а не только совпадение начала).
    """
    stmt = select(Appointment.id).where(
        owner_column == owner_id,
        Appointment.status != AppointmentStatus.CANCELLED,
        and_(Appointment.starts_at < ends_at, Appointment.ends_at > starts_at),
    )
    if exclude_id is not None:
        stmt = stmt.where(Appointment.id != exclude_id)
    return await session.scalar(stmt.limit(1)) is not None


async def _validate_slot(
    session: AsyncSession,
    *,
    doctor: Doctor,
    patient_id: int,
    starts_at: datetime,
    ends_at: datetime,
    now: datetime,
    exclude_id: int | None = None,
) -> None:
    """Правила П1-П5 для интервала записи. Врач (и пациент) должны быть уже заблокированы (П6).

    ``exclude_id`` - id переносимой записи: сама себе она не мешает.
    """
    if not await _doctor_is_active(session, doctor):  # П5
        raise BusinessRuleError("The doctor is not available for new appointments")

    schedule = _schedule_of(doctor)
    tz = get_settings().clinic_tz
    try:
        scheduling.validate_booking_window(starts_at, now=now)  # П1
        scheduling.validate_within_shift(starts_at, ends_at, schedule, tz)  # П2: рабочий день и смена
        scheduling.validate_slot_grid(starts_at, ends_at, schedule, tz)  # П2: сетка слотов
    except scheduling.SchedulingError as exc:
        raise BusinessRuleError(str(exc)) from exc

    if await _has_overlap(session, Appointment.doctor_id, doctor.id, starts_at, ends_at, exclude_id):  # П3
        raise ConflictError("The doctor already has an appointment at this time")
    if await _has_overlap(session, Appointment.patient_id, patient_id, starts_at, ends_at, exclude_id):  # П4
        raise ConflictError("The patient already has an appointment at this time")


async def _commit(session: AsyncSession, conflict_message: str) -> None:
    """Фиксирует транзакцию; нарушение уникальности (гонка, П6) превращает в 409."""
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        if is_unique_violation(exc):
            raise ConflictError(conflict_message) from exc
        raise


async def _load_visible(
    session: AsyncSession, actor: Actor, appointment_id: int, *, lock: bool
) -> Appointment:
    """Загружает запись, если она видна пользователю; иначе ``NotFoundError`` (404).

    При ``lock=True`` строка блокируется (``FOR UPDATE``) и перечитывается из БД.
    """
    stmt = select(Appointment).where(Appointment.id == appointment_id)
    clause = actor.visibility_clause()
    if clause is not None:
        stmt = stmt.where(clause)
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    appointment = await session.scalar(stmt)
    if appointment is None:
        raise NotFoundError("Appointment not found")
    return appointment


def _ensure_patient_deadline(actor: Actor, appointment: Appointment, *, now: datetime) -> None:
    """Пациент может изменить или отменить запись не позже чем за 2 часа до начала (ровно за 2 часа - можно)."""
    if actor.role == UserRole.PATIENT and appointment.starts_at - now < PATIENT_CHANGE_DEADLINE:
        raise PermissionDeniedError(
            "Patients can only modify or cancel an appointment at least 2 hours before it starts; "
            "please contact the clinic"
        )


def _requested_interval(appointment: Appointment, data: AppointmentUpdate) -> tuple[datetime, datetime]:
    """Итоговый интервал после PATCH.

    * переданы обе границы - берутся как есть;
    * только ``starts_at`` - запись переносится с сохранением длительности;
    * только ``ends_at`` - меняется длительность, начало прежнее;
    * ни одной - интервал прежний.
    """
    start = data.starts_at if data.starts_at is not None else appointment.starts_at
    if data.ends_at is not None:
        end = data.ends_at
    elif data.starts_at is not None:
        try:
            end = start + (appointment.ends_at - appointment.starts_at)
        except OverflowError:
            raise BusinessRuleError("starts_at is out of the supported range") from None
    else:
        end = appointment.ends_at
    if end <= start:
        raise BusinessRuleError("ends_at must be later than starts_at")
    return start, end


# --------------------------------------------------------------------------- #
# Публичные операции
# --------------------------------------------------------------------------- #


async def create_appointment(
    session: AsyncSession, user: User, data: AppointmentCreate, *, now: datetime
) -> Appointment:
    """Создаёт запись (статус ``scheduled``, ``created_by`` = текущий пользователь - П9).

    Права: admin / registrar - любому пациенту; patient - только на свой профиль;
    doctor - не может.
    """
    if user.role not in BOOKING_ROLES:
        raise PermissionDeniedError("Insufficient permissions to create appointments")
    actor = await load_actor(session, user)
    if user.role == UserRole.PATIENT and (actor.patient_id is None or data.patient_id != actor.patient_id):
        raise PermissionDeniedError("Patients can only book appointments for their own profile")

    doctor = await _lock_doctor(session, data.doctor_id)  # П6: до проверки П3
    await _lock_patient(session, data.patient_id)
    await _validate_slot(
        session,
        doctor=doctor,
        patient_id=data.patient_id,
        starts_at=data.starts_at,
        ends_at=data.ends_at,
        now=now,
    )

    appointment = Appointment(
        doctor_id=doctor.id,
        patient_id=data.patient_id,
        starts_at=data.starts_at,
        ends_at=data.ends_at,
        status=AppointmentStatus.SCHEDULED,
        reason=data.reason,
        created_by=user.id,
    )
    session.add(appointment)
    await _commit(session, "This time slot has just been taken; please choose another one")
    return appointment


async def get_appointment(session: AsyncSession, user: User, appointment_id: int) -> Appointment:
    """Возвращает запись, если она видна пользователю; иначе ``NotFoundError`` (404)."""
    actor = await load_actor(session, user)
    return await _load_visible(session, actor, appointment_id, lock=False)


async def list_appointments(
    session: AsyncSession,
    user: User,
    filters: AppointmentFilters,
    *,
    limit: int,
    offset: int,
) -> list[Appointment]:
    """Список записей, доступных пользователю, с фильтрами и пагинацией; сортировка по ``starts_at``.

    Фильтры комбинируются с ограничением видимости через AND: врач, запросивший чужой
    ``doctor_id``, получает пустой список (а не 403 и не чужие данные). Для стабильной
    пагинации при равных ``starts_at`` добавлена сортировка по ``id``.
    """
    if filters.date_from is not None and filters.date_to is not None and filters.date_from > filters.date_to:
        raise BusinessRuleError("date_from must not be later than date_to")

    actor = await load_actor(session, user)
    stmt = select(Appointment)
    clause = actor.visibility_clause()
    if clause is not None:
        stmt = stmt.where(clause)
    if filters.doctor_id is not None:
        stmt = stmt.where(Appointment.doctor_id == filters.doctor_id)
    if filters.patient_id is not None:
        stmt = stmt.where(Appointment.patient_id == filters.patient_id)
    if filters.status is not None:
        stmt = stmt.where(Appointment.status == filters.status)

    tz = get_settings().clinic_tz
    if filters.date_from is not None:
        day_start, _ = scheduling.local_day_bounds(filters.date_from, tz)
        stmt = stmt.where(Appointment.starts_at >= day_start)
    if filters.date_to is not None:
        _, day_end = scheduling.local_day_bounds(filters.date_to, tz)
        stmt = stmt.where(Appointment.starts_at < day_end)

    stmt = stmt.order_by(Appointment.starts_at, Appointment.id).limit(limit).offset(offset)
    return list((await session.scalars(stmt)).all())


async def update_appointment(
    session: AsyncSession,
    user: User,
    appointment_id: int,
    data: AppointmentUpdate,
    *,
    now: datetime,
) -> Appointment:
    """PATCH: перенос (``starts_at`` / ``ends_at``) и/или смена причины - только для ``scheduled`` (П8).

    * поле ``status`` в PATCH запрещено (для этого есть /cancel, /complete, /no-show);
    * при изменении интервала выполняются те же проверки П1-П6, что и при создании,
      сама запись из поиска пересечений исключается;
    * пациент: только своя запись и не позже чем за 2 часа до её (прежнего) начала;
    * изменение только причины интервал не трогает, поэтому П1-П6 в этом случае не проверяются.
    """
    fields = data.model_fields_set
    if "status" in fields:
        raise BusinessRuleError("Status cannot be changed via PATCH; use /cancel, /complete or /no-show")
    if not fields:
        raise BusinessRuleError("No fields to update")
    if user.role not in BOOKING_ROLES:
        raise PermissionDeniedError("Insufficient permissions to modify appointments")

    actor = await load_actor(session, user)
    appointment = await _load_visible(session, actor, appointment_id, lock=True)  # запись -> врач -> пациент
    if appointment.status != AppointmentStatus.SCHEDULED:
        raise ConflictError("Only scheduled appointments can be modified")
    _ensure_patient_deadline(actor, appointment, now=now)

    new_start, new_end = _requested_interval(appointment, data)
    if (new_start, new_end) != (appointment.starts_at, appointment.ends_at):
        doctor = await _lock_doctor(session, appointment.doctor_id)  # П6
        await _lock_patient(session, appointment.patient_id)
        await _validate_slot(
            session,
            doctor=doctor,
            patient_id=appointment.patient_id,
            starts_at=new_start,
            ends_at=new_end,
            now=now,
            exclude_id=appointment.id,
        )
        appointment.starts_at = new_start
        appointment.ends_at = new_end
    if "reason" in fields:
        appointment.reason = data.reason

    await _commit(session, "This time slot has just been taken; please choose another one")
    return appointment


async def change_status(
    session: AsyncSession,
    user: User,
    appointment_id: int,
    target: AppointmentStatus,
    *,
    now: datetime,
) -> Appointment:
    """Переводит запись в ``cancelled`` / ``completed`` / ``no_show`` (П7).

    * ``cancelled``: admin / registrar - любую; patient - свою и не позже чем за 2 часа до начала;
    * ``completed`` / ``no_show``: только врач и только свою запись, и только когда приём уже начался;
    * допустим только переход из ``scheduled``; повторная смена статуса - 409.
    """
    if user.role not in STATUS_CHANGE_ROLES.get(target, frozenset()):
        raise PermissionDeniedError("Insufficient permissions to set this status")

    actor = await load_actor(session, user)
    appointment = await _load_visible(session, actor, appointment_id, lock=True)
    if target not in ALLOWED_TRANSITIONS[appointment.status]:
        raise ConflictError(f"Cannot change status from '{appointment.status.value}' to '{target.value}'")
    if target in (AppointmentStatus.COMPLETED, AppointmentStatus.NO_SHOW) and now < appointment.starts_at:
        raise BusinessRuleError("The appointment has not started yet")
    if target == AppointmentStatus.CANCELLED:
        _ensure_patient_deadline(actor, appointment, now=now)

    appointment.status = target
    await _commit(session, "The appointment was modified concurrently; please retry")
    return appointment


async def list_free_slots(
    session: AsyncSession, doctor_id: int, day: date, *, now: datetime
) -> list[scheduling.Slot]:
    """Свободные слоты врача на местную дату ``day``.

    Деактивированный врач слотов не имеет (пустой список). Ответ информационный:
    окончательные проверки выполняет ``create_appointment``.
    """
    doctor = await session.get(Doctor, doctor_id)
    if doctor is None:
        raise NotFoundError("Doctor not found")
    if not await _doctor_is_active(session, doctor):
        return []

    tz = get_settings().clinic_tz
    day_start, day_end = scheduling.local_day_bounds(day, tz)
    rows = await session.execute(
        select(Appointment.starts_at, Appointment.ends_at).where(
            Appointment.doctor_id == doctor.id,
            Appointment.status != AppointmentStatus.CANCELLED,
            Appointment.starts_at < day_end,
            Appointment.ends_at > day_start,
        )
    )
    busy = [(starts_at, ends_at) for starts_at, ends_at in rows.all()]
    return scheduling.free_slots(_schedule_of(doctor), day, tz, busy, now=now)
```

### app/routers/auth.py

```python
"""Аутентификация: регистрация пациента, вход по паролю, текущий пользователь.

* ``POST /api/auth/register`` создаёт ``User(role=patient)`` и ``Patient`` в одной транзакции.
  Если пациент с таким полисом уже существует, аккаунт к нему НЕ привязывается (иначе любой,
  кто знает номер полиса, получил бы доступ к чужой медкарте): ответ 409, привязку выполняет
  регистратор.
* ``POST /api/auth/login`` отвечает одинаковым 401 на «нет пользователя», «неверный пароль» и
  «аккаунт деактивирован». Проверка bcrypt выполняется всегда (при отсутствии пользователя - по
  фиктивному хэшу той же стоимости), чтобы по времени ответа нельзя было определить, существует
  ли email.
"""

import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.errors import ConflictError, is_unique_violation
from app.models import Patient, User, UserRole
from app.schemas import (
    BirthDate,
    Email,
    FullName,
    LoginIn,
    Password,
    PhoneE164,
    PolicyNumber,
    TokenOut,
    UserOut,
)
from app.security import (
    CurrentUser,
    create_access_token,
    hash_password,
    hash_password_async,
    verify_password_async,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]

# Хэш случайного пароля: нужен только для выравнивания времени ответа, когда пользователь не
# найден. Считается один раз при импорте модуля (~0.25 с) с той же стоимостью bcrypt, что у
# настоящих хэшей, поскольку берётся из той же ``hash_password``.
_DUMMY_PASSWORD_HASH: str = hash_password(secrets.token_urlsafe(32))


class RegisterIn(BaseModel):
    """Тело самостоятельной регистрации пациента. Роль не принимается: всегда ``patient``."""

    model_config = ConfigDict(extra="forbid")

    email: Email
    password: Password
    full_name: FullName
    birth_date: BirthDate
    phone: PhoneE164 | None = None
    policy_number: PolicyNumber


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def register(data: RegisterIn, session: SessionDep) -> User:
    """Регистрирует пациента: создаёт ``User(role=patient)`` и ``Patient`` одной транзакцией."""
    if await session.scalar(select(User.id).where(User.email == data.email)) is not None:
        raise ConflictError("Email is already registered")
    if await session.scalar(select(Patient.id).where(Patient.policy_number == data.policy_number)) is not None:
        raise ConflictError(
            "A patient with this policy number already exists; "
            "please contact the clinic registrar to link your account"
        )

    # bcrypt - после проверок и до записи: ни лишней нагрузки на CPU для заведомо отказных
    # запросов, ни блокирующего вызова в event loop.
    password_hash = await hash_password_async(data.password.get_secret_value())

    user = User(
        email=data.email,
        password_hash=password_hash,
        full_name=data.full_name,
        role=UserRole.PATIENT,
        is_active=True,
    )
    session.add(user)
    try:
        await session.flush()  # нужен user.id для Patient.user_id
        session.add(
            Patient(
                user_id=user.id,
                full_name=data.full_name,
                birth_date=data.birth_date,
                phone=data.phone,
                policy_number=data.policy_number,
            )
        )
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        if is_unique_violation(exc):  # гонка: email или полис заняли между проверкой и записью
            raise ConflictError("Email or policy number is already registered") from exc
        raise
    return user


@router.post("/login", response_model=TokenOut)
async def login(data: LoginIn, session: SessionDep) -> TokenOut:
    """Выдаёт JWT по email и паролю. На любую причину отказа - один и тот же 401."""
    user = await session.scalar(select(User).where(User.email == data.email))
    password_hash = user.password_hash if user is not None else _DUMMY_PASSWORD_HASH
    password_ok = await verify_password_async(data.password.get_secret_value(), password_hash)

    if user is None or not password_ok or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return TokenOut(
        access_token=create_access_token(user.id, user.role),
        expires_in=get_settings().jwt_ttl_minutes * 60,
    )


@router.get("/me", response_model=UserOut)
async def me(user: CurrentUser) -> User:
    """Текущий пользователь (по токену)."""
    return user
```

### app/routers/appointments.py

```python
"""Роуты записей на приём и свободных слотов врача.

Роуты тонкие: разбирают параметры, проверяют роль через ``require_roles`` и вызывают
``app.services.appointments``, где находятся бизнес-правила, проверка прав на уровне
объектов и коммит транзакции. Доменные ошибки превращаются в HTTP-ответы обработчиками
из ``app.errors``.

Текущее время берётся из зависимости ``get_now``: в тестах её можно подменить через
``app.dependency_overrides[get_now]``.
"""

from datetime import date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Appointment, AppointmentStatus, User, UserRole, utc_now
from app.schemas import AppointmentCreate, AppointmentOut, AppointmentUpdate, UTCDatetime
from app.security import CurrentUser, get_current_user, require_roles
from app.services import appointments as service
from app.services import scheduling
from app.services.appointments import BOOKING_ROLES, AppointmentFilters

router = APIRouter(prefix="/api", tags=["appointments"])

_MAX_ID = 2_147_483_647  # граница INTEGER в PostgreSQL (как EntityId в схемах)
_MIN_QUERY_DATE = date(1970, 1, 1)  # разумные границы дат в параметрах: защита от OverflowError
_MAX_QUERY_DATE = date(2100, 12, 31)

SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def get_now() -> datetime:
    """Текущий момент (aware UTC). Вынесено в зависимость, чтобы тесты могли «остановить время»."""
    return utc_now()


NowDep = Annotated[datetime, Depends(get_now)]
BookingUser = Annotated[User, Depends(require_roles(*BOOKING_ROLES))]  # admin, registrar, patient
DoctorUser = Annotated[User, Depends(require_roles(UserRole.DOCTOR))]

AppointmentId = Annotated[int, Path(gt=0, le=_MAX_ID)]
DoctorId = Annotated[int, Path(gt=0, le=_MAX_ID)]
OptionalId = Annotated[int | None, Query(gt=0, le=_MAX_ID)]
OptionalDate = Annotated[date | None, Query(ge=_MIN_QUERY_DATE, le=_MAX_QUERY_DATE)]


class SlotOut(BaseModel):
    """Свободный слот врача: полуоткрытый интервал ``[starts_at, ends_at)``, UTC."""

    model_config = ConfigDict(from_attributes=True)

    starts_at: UTCDatetime
    ends_at: UTCDatetime


@router.get("/appointments", response_model=list[AppointmentOut])
async def list_appointments(
    user: CurrentUser,
    session: SessionDep,
    doctor_id: OptionalId = None,
    patient_id: OptionalId = None,
    status_filter: Annotated[AppointmentStatus | None, Query(alias="status")] = None,
    date_from: OptionalDate = None,
    date_to: OptionalDate = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0, le=_MAX_ID)] = 0,
) -> list[Appointment]:
    """Список записей, доступных пользователю. ``date_from`` / ``date_to`` - местные даты клиники."""
    filters = AppointmentFilters(
        doctor_id=doctor_id,
        patient_id=patient_id,
        status=status_filter,
        date_from=date_from,
        date_to=date_to,
    )
    return await service.list_appointments(session, user, filters, limit=limit, offset=offset)


@router.get("/appointments/{appointment_id}", response_model=AppointmentOut)
async def get_appointment(appointment_id: AppointmentId, user: CurrentUser, session: SessionDep) -> Appointment:
    """Одна запись; чужая запись для врача и пациента - 404."""
    return await service.get_appointment(session, user, appointment_id)


@router.post("/appointments", response_model=AppointmentOut, status_code=status.HTTP_201_CREATED)
async def create_appointment(
    data: AppointmentCreate, user: BookingUser, session: SessionDep, now: NowDep
) -> Appointment:
    """Создаёт запись. ``created_by`` берётся из токена, статус всегда ``scheduled``."""
    return await service.create_appointment(session, user, data, now=now)


@router.patch("/appointments/{appointment_id}", response_model=AppointmentOut)
async def update_appointment(
    appointment_id: AppointmentId,
    data: AppointmentUpdate,
    user: BookingUser,
    session: SessionDep,
    now: NowDep,
) -> Appointment:
    """Перенос (``starts_at`` / ``ends_at``) и/или смена причины; статус через PATCH менять нельзя."""
    return await service.update_appointment(session, user, appointment_id, data, now=now)


@router.post("/appointments/{appointment_id}/cancel", response_model=AppointmentOut)
async def cancel_appointment(
    appointment_id: AppointmentId, user: BookingUser, session: SessionDep, now: NowDep
) -> Appointment:
    """Отмена записи (admin / registrar; patient - свою, не позже чем за 2 часа до начала)."""
    return await service.change_status(session, user, appointment_id, AppointmentStatus.CANCELLED, now=now)


@router.post("/appointments/{appointment_id}/complete", response_model=AppointmentOut)
async def complete_appointment(
    appointment_id: AppointmentId, user: DoctorUser, session: SessionDep, now: NowDep
) -> Appointment:
    """Приём состоялся (врач - только свою запись, и только после начала приёма)."""
    return await service.change_status(session, user, appointment_id, AppointmentStatus.COMPLETED, now=now)


@router.post("/appointments/{appointment_id}/no-show", response_model=AppointmentOut)
async def no_show_appointment(
    appointment_id: AppointmentId, user: DoctorUser, session: SessionDep, now: NowDep
) -> Appointment:
    """Пациент не явился (врач - только свою запись, и только после начала приёма)."""
    return await service.change_status(session, user, appointment_id, AppointmentStatus.NO_SHOW, now=now)


@router.get(
    "/doctors/{doctor_id}/slots",
    response_model=list[SlotOut],
    dependencies=[Depends(get_current_user)],  # слоты видят все аутентифицированные пользователи
)
async def get_doctor_slots(
    doctor_id: DoctorId,
    session: SessionDep,
    now: NowDep,
    day: Annotated[date, Query(alias="date", ge=_MIN_QUERY_DATE, le=_MAX_QUERY_DATE)],
) -> list[scheduling.Slot]:
    """Свободные слоты врача на местную дату клиники (``?date=YYYY-MM-DD``)."""
    return await service.list_free_slots(session, doctor_id, day, now=now)
```

### app/main.py

```python
"""Фабрика приложения FastAPI.

``create_app()`` собирает приложение: подключает роутеры и обработчики доменных ошибок,
а в ``lifespan`` закрывает пул соединений с БД при остановке. Запуск:
``uvicorn app.main:app`` (или ``uvicorn --factory app.main:create_app``).
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.db import dispose_engine
from app.errors import register_exception_handlers
from app.routers import appointments as appointments_router
from app.routers import auth as auth_router


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Жизненный цикл приложения: при остановке освобождает пул соединений."""
    try:
        yield
    finally:
        await dispose_engine()


def create_app() -> FastAPI:
    """Создаёт и настраивает экземпляр приложения."""
    app = FastAPI(title="Clinic management system", version="1.0.0", lifespan=lifespan)
    register_exception_handlers(app)
    app.include_router(auth_router.router)
    app.include_router(appointments_router.router)

    @app.get("/api/health", tags=["system"])
    async def health() -> dict[str, str]:
        """Проверка живости сервиса (без обращения к БД)."""
        return {"status": "ok"}

    return app


app = create_app()
```

## Допущения

- **Ответы на нарушения правил.** 404 - нет объекта (в т.ч. чужая запись врача/пациента, несуществующие врач или пациент в теле POST); 403 - запрещено роли/владельцу (в т.ч. окно «менее 2 часов» для пациента); 409 - пересечение с записью врача/пациента (П3, П4), недопустимый переход статуса и правка не-`scheduled` записи; 422 - П1, П2, П5, «приём ещё не начался», `status` в PATCH, пустой PATCH, `date_from > date_to`.
- **Матрица прав строго по тексту задания:** создавать, переносить и отменять - admin, registrar, patient (пациент только своё); `completed` / `no_show` ставит только врач и только свою запись (admin и registrar - нет); врач не может создавать, переносить и отменять. Множества ролей вынесены в константы `BOOKING_ROLES` и `STATUS_CHANGE_ROLES` и меняются в одном месте.
- **Окно «за 2 часа»** применяется к любому PATCH пациента (в т.ч. к смене одной причины) и к отмене; ровно за 2 часа до начала - ещё можно. Оно считается от прежнего начала записи; новое начало при переносе проверяется обычными правилами П1-П6.
- **PATCH с одной границей:** только `starts_at` - запись сдвигается с сохранением длительности; только `ends_at` - меняется длительность. Если интервал в итоге не изменился (например, только `reason`), П1-П6 не проверяются.
- **П1:** начало строго позже `now`, верхняя граница `now + 90 суток` включительна. Слоты в `/slots` фильтруются тем же окном, чтобы не предлагать то, что нельзя забронировать.
- **П5 при переносе:** запись к деактивированному врачу нельзя перенести (но можно отменить); `/slots` у деактивированного врача возвращает пустой список.
- **Гонки:** помимо строки врача (П6) блокируется строка пациента - иначе П4 не защищён от одновременной записи одного пациента к разным врачам. Порядок блокировок везде «запись -> врач -> пациент». Предполагается уровень изоляции READ COMMITTED (по умолчанию в PostgreSQL).
- **Регистрация:** телефон необязателен (как в `PatientCreate`); ответ - `UserOut` со статусом 201 (токен выдаёт `/login`). Схема `RegisterIn` и ответ `SlotOut` определены в самих роутерах, `schemas.py` не менялся. Фиктивный bcrypt-хэш считается один раз при импорте `app/routers/auth.py` (~0,25 с).
- **Формат списка:** `GET /api/appointments` возвращает JSON-массив без обёртки и без общего количества; сортировка `starts_at, id`. `date_from` / `date_to` - местные даты клиники, включительно, по полю `starts_at`; допустимый диапазон дат в параметрах 1970-01-01 ... 2100-12-31.
- **Время в тестах:** «сейчас» для роутов приходит из зависимости `get_now` (`app.routers.appointments.get_now`) - её подменяют через `app.dependency_overrides`. Часовой пояс клиники берётся из `get_settings().clinic_tz` внутри сервисного слоя.
- **Перевод часов (DST):** сетка считается по «настенному» времени, слоты в «пропавшем» часе не поддерживаются (для Europe/Moscow неактуально).
- Параметры запроса (`limit` 1..100, `offset`, `status`, `doctor_id`, `patient_id`, `date`, `date_from`, `date_to`) и тела запросов валидируются средствами FastAPI/Pydantic (стандартный 422 со списком ошибок в `detail`); доменные ошибки возвращают `{"detail": "строка"}`.
- Лимитов частоты запросов (rate limiting) на `/login` и `/register` нет - вне задачи.

## Что проверить на ревью особенно внимательно

1. **Блокировки и гонки (П6).** `FOR UPDATE` в SQLite - пустая операция, поэтому корректность блокировок проверяется только на PostgreSQL (два параллельных POST на один слот / на пересекающиеся слоты; один пациент - два врача). Проверьте порядок «запись -> врач -> пациент» и отсутствие взаимных блокировок.
2. **`is_unique_violation` (`app/errors.py`).** Опирается на атрибуты `sqlstate` / `pgcode` (адаптер asyncpg в SQLAlchemy) и `sqlite_errorcode` / текст ошибки SQLite; при смене драйвера или версии SQLAlchemy классификация может перестать срабатывать, и гонка станет 500 вместо 409. Не-уникальные `IntegrityError` намеренно пробрасываются дальше.
3. **SQL-условие пересечения** в `_has_overlap` дублирует `scheduling.intervals_overlap`; нужно убедиться, что они совпадают (полуоткрытые интервалы, соседние записи не конфликтуют) и что строковое сравнение времени в SQLite корректно (все значения хранятся в UTC через `UTCDateTime`).
4. **Права и скрытие существования.** Условие видимости входит в SQL (`_load_visible`), поэтому чужая запись даёт 404 и не блокируется. Но грубая проверка роли в роутах даёт 403 до поиска записи: врач на `PATCH` / `cancel` и пациент на `complete` / `no-show` получают 403 независимо от id - проверьте, что это соответствует ожиданиям (по ТЗ 404 требуется для чужой записи при допустимой роли).
5. **Выбор статусов ответа:** 409 для недопустимого перехода и 422 для «приём ещё не начался» - спорное решение (альтернатива - везде 422); 403 для окна в 2 часа.
6. **Проверка `status` в PATCH.** `AppointmentUpdate` содержит поле `status`; сервис отвергает его явно (422), иначе пациент мог бы менять статус в обход правил П7 и прав.
7. **`expire_on_commit=False` и ответ после `commit()`.** `AppointmentOut` / `UserOut` читаются из объектов после коммита; это работает, потому что значения по умолчанию (`status`, `created_at`, `is_active`) задаются на стороне Python и попадают в объект при flush. При переходе на `server_default` без явной установки нужен `refresh`.
8. **Тайминг логина.** Фиктивный хэш создаётся той же `hash_password`, поэтому стоимость совпадает; для пароля длиннее 72 байт `verify_password` быстро возвращает `False` и в случае существующего, и несуществующего пользователя (различия по email это не раскрывает).
9. **Регистрация раскрывает занятость email и полиса** через 409 (так требует задание); если это нежелательно, нужен другой механизм (например, письмо на почту).
10. **Крайние значения дат.** `UTCDatetime._to_utc` в `schemas.py` при экстремальных значениях (например, `0001-01-01T00:00:00+05:00`) может бросить `OverflowError` (это не `ValueError`) и дать 500 - файл не менялся; в сервисном слое и параметрах запросов такие значения отсекаются раньше.
11. **Блокирующих вызовов в async-коде нет:** bcrypt вынесен в `asyncio.to_thread`; единственное блокирующее вычисление - фиктивный хэш при импорте модуля.

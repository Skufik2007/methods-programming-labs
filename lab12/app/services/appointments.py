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

from sqlalchemy import ColumnElement, and_, false, select, update
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


async def _lock_row(session: AsyncSession, model: type[Doctor] | type[Appointment], row_id: int) -> None:
    """Эмуляция ``FOR UPDATE`` для SQLite, где этот оператор игнорируется.

    Пустой UPDATE (``id = id``) открывает пишущую транзакцию и берёт блокировку записи на всю
    базу до commit/rollback: конкурирующая операция ждёт её, а потом видит уже
    зафиксированные данные. Без этого два одновременных запроса на пересекающееся время
    к одному врачу проходили оба. В PostgreSQL ничего не делает: там работает ``FOR UPDATE``.
    """
    if session.bind.dialect.name != "sqlite":
        return
    await session.execute(
        update(model).where(model.id == row_id).values(id=model.id).execution_options(synchronize_session=False)
    )


async def _lock_doctor(session: AsyncSession, doctor_id: int) -> Doctor:
    """Читает врача и блокирует его строку до конца транзакции (П6)."""
    await _lock_row(session, Doctor, doctor_id)
    stmt = select(Doctor).where(Doctor.id == doctor_id).with_for_update().execution_options(populate_existing=True)
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


async def _load_visible(session: AsyncSession, actor: Actor, appointment_id: int, *, lock: bool) -> Appointment:
    """Загружает запись, если она видна пользователю; иначе ``NotFoundError`` (404).

    При ``lock=True`` строка блокируется (``FOR UPDATE``) и перечитывается из БД.
    """
    stmt = select(Appointment).where(Appointment.id == appointment_id)
    clause = actor.visibility_clause()
    if clause is not None:
        stmt = stmt.where(clause)
    if lock:
        await _lock_row(session, Appointment, appointment_id)
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
        # Начавшийся или прошедший приём не отменяют: иначе неявка превращается в «отмену»
        # и пропадает из статистики no_show, а слот в прошлом освобождается задним числом.
        if now >= appointment.starts_at:
            raise BusinessRuleError("The appointment has already started; it can only be completed or marked no-show")
        _ensure_patient_deadline(actor, appointment, now=now)

    appointment.status = target
    await _commit(session, "The appointment was modified concurrently; please retry")
    return appointment


async def list_free_slots(session: AsyncSession, doctor_id: int, day: date, *, now: datetime) -> list[scheduling.Slot]:
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

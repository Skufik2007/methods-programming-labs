"""Профили врачей: список, чтение, создание, изменение графика.

* Читать врачей могут все аутентифицированные; в списке нет врачей с деактивированным
  аккаунтом, но прямой запрос по id возвращает любого (у пациента могут остаться записи).
* Создаёт и меняет профили только администратор.
* Смена графика (дни, смена, длина слота) не должна оставлять будущие записи ``scheduled``
  вне смены или вне сетки. Проверка использует те же чистые функции ``scheduling``, что и
  создание записи, поэтому правила не расходятся. Строка врача блокируется тем же способом,
  что при записи на приём, так что параллельная запись не пройдёт по старому графику.

ФИО врача лежит у пользователя; ответы собираются одним JOIN-запросом (без N+1).
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.errors import BusinessRuleError, ConflictError, NotFoundError
from app.models import Appointment, AppointmentStatus, Doctor, User, UserRole
from app.schemas import DoctorCreate, DoctorUpdate
from app.services import scheduling
from app.services.appointments import _lock_doctor, _schedule_of  # noqa: PLC2701 - единый протокол блокировок
from app.services.dbutil import ci_equals, commit_or_conflict
from app.services.users import ensure_admin, lock_user


@dataclass(frozen=True, slots=True)
class DoctorView:
    """Профиль врача и ФИО его пользователя."""

    doctor: Doctor
    full_name: str


async def list_doctors(session: AsyncSession, *, specialty: str | None) -> list[DoctorView]:
    """Врачи с активным аккаунтом; ``specialty`` - точное совпадение без учёта регистра.

    Сортировка по ФИО, затем по ``id``. Один запрос с JOIN.
    """
    stmt = (
        select(Doctor, User.full_name)
        .select_from(Doctor)
        .join(User, User.id == Doctor.user_id)
        .where(User.is_active.is_(True))
    )
    term = (specialty or "").strip()
    if term:
        stmt = stmt.where(ci_equals(Doctor.specialty, term))
    rows = await session.execute(stmt.order_by(User.full_name, Doctor.id))
    return [DoctorView(doctor=doctor, full_name=full_name) for doctor, full_name in rows.all()]


async def get_doctor(session: AsyncSession, doctor_id: int) -> DoctorView:
    """Врач по id (в том числе с деактивированным аккаунтом); нет такого - 404."""
    stmt = (
        select(Doctor, User.full_name)
        .select_from(Doctor)
        .join(User, User.id == Doctor.user_id)
        .where(Doctor.id == doctor_id)
    )
    row = (await session.execute(stmt)).first()
    if row is None:
        raise NotFoundError("Doctor not found")
    return DoctorView(doctor=row[0], full_name=row[1])


async def create_doctor(session: AsyncSession, actor: User, data: DoctorCreate) -> DoctorView:
    """Создаёт профиль врача для пользователя с ролью ``doctor``, у которого профиля ещё нет.

    Нет пользователя - 404; роль не ``doctor`` - 422; профиль уже есть - 409.
    """
    ensure_admin(actor)
    user = await lock_user(session, data.user_id)  # сериализация со сменой роли
    if user.role != UserRole.DOCTOR:
        raise BusinessRuleError("The user must have the 'doctor' role")
    if await session.scalar(select(Doctor.id).where(Doctor.user_id == user.id)) is not None:
        raise ConflictError("The user already has a doctor profile")

    doctor = Doctor(
        user_id=user.id,
        specialty=data.specialty,
        cabinet=data.cabinet,
        slot_minutes=data.slot_minutes,
        work_start=data.work_start,
        work_end=data.work_end,
        work_days=list(data.work_days),
    )
    session.add(doctor)
    await commit_or_conflict(session, "The user already has a doctor profile")
    return DoctorView(doctor=doctor, full_name=user.full_name)


async def _count_incompatible_appointments(
    session: AsyncSession, doctor_id: int, schedule: scheduling.Schedule, *, now: datetime
) -> int:
    """Сколько будущих записей ``scheduled`` врача не укладывается в смену или сетку ``schedule``.

    Из БД читаются только два столбца времени; правила применяются теми же функциями
    ``scheduling.validate_within_shift`` / ``validate_slot_grid``, что и при записи
    (в том числе «длительность - от 1 до 4 слотов»). «Будущая» - начало строго после ``now``.
    """
    tz = get_settings().clinic_tz
    rows = await session.execute(
        select(Appointment.starts_at, Appointment.ends_at).where(
            Appointment.doctor_id == doctor_id,
            Appointment.status == AppointmentStatus.SCHEDULED,
            Appointment.starts_at > now,
        )
    )
    incompatible = 0
    for starts_at, ends_at in rows.all():
        try:
            scheduling.validate_within_shift(starts_at, ends_at, schedule, tz)
            scheduling.validate_slot_grid(starts_at, ends_at, schedule, tz)
        except scheduling.SchedulingError:
            incompatible += 1
    return incompatible


async def update_doctor(
    session: AsyncSession, actor: User, doctor_id: int, data: DoctorUpdate, *, now: datetime
) -> DoctorView:
    """PATCH: кабинет, специальность, график.

    * пустое тело - 422; нет врача - 404;
    * ``work_start >= work_end`` после слияния с сохранёнными значениями - 422;
    * если график (дни, смена, длина слота) меняется и из-за этого ``N`` будущих записей
      ``scheduled`` оказываются вне смены или вне сетки - 409 с числом ``N`` в сообщении,
      график остаётся прежним. Смена только кабинета/специальности записи не проверяет.
    """
    ensure_admin(actor)
    if not data.model_fields_set:
        raise BusinessRuleError("No fields to update")

    doctor = await _lock_doctor(session, doctor_id)

    new_start = data.work_start if data.work_start is not None else doctor.work_start
    new_end = data.work_end if data.work_end is not None else doctor.work_end
    if new_start >= new_end:
        raise BusinessRuleError("work_start must be earlier than work_end (overnight shifts are not supported)")
    new_schedule = scheduling.Schedule(
        work_days=frozenset(data.work_days if data.work_days is not None else doctor.work_days),
        work_start=new_start,
        work_end=new_end,
        slot_minutes=data.slot_minutes if data.slot_minutes is not None else doctor.slot_minutes,
    )

    if new_schedule != _schedule_of(doctor):
        incompatible = await _count_incompatible_appointments(session, doctor.id, new_schedule, now=now)
        if incompatible:
            raise ConflictError(
                f"The new schedule leaves {incompatible} upcoming scheduled appointment(s) outside the shift "
                "or the slot grid; reschedule or cancel them first"
            )

    if data.specialty is not None:
        doctor.specialty = data.specialty
    if data.cabinet is not None:
        doctor.cabinet = data.cabinet
    if data.slot_minutes is not None:
        doctor.slot_minutes = data.slot_minutes
    if data.work_start is not None:
        doctor.work_start = data.work_start
    if data.work_end is not None:
        doctor.work_end = data.work_end
    if data.work_days is not None:
        doctor.work_days = list(data.work_days)  # JSON-колонку присваиваем целиком

    full_name = await session.scalar(select(User.full_name).where(User.id == doctor.user_id))
    await commit_or_conflict(session, "The doctor was modified concurrently; please retry")
    return DoctorView(doctor=doctor, full_name=full_name or "")

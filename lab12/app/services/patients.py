"""Пациенты: поиск, чтение, создание, изменение и привязка аккаунта.

Права на уровне объектов:

* admin / registrar - любой пациент; создание и изменение - только они; список - они и врач
  (врачу - только свои пациенты);
* doctor - только пациент, у которого есть хотя бы одна запись к этому врачу в любом
  статусе, кроме ``cancelled``; иначе 404 (чужой пациент неотличим от несуществующего);
* patient - только свой профиль (``/me`` или собственный id; чужой id - 404).

Условие видимости входит в SQL-запрос, поэтому чужие строки не читаются.

Привязка аккаунта (``user_id``): пользователь должен существовать (404), иметь роль
``patient`` (422) и не быть привязанным к другому пациенту (409).
"""

from sqlalchemy import ColumnElement, false, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import BusinessRuleError, ConflictError, NotFoundError, PermissionDeniedError
from app.models import Appointment, AppointmentStatus, Patient, User, UserRole
from app.schemas import PatientCreate, PatientUpdate
from app.services.appointments import STAFF_ROLES, Actor, load_actor
from app.services.dbutil import ci_contains, commit_or_conflict
from app.services.users import lock_user

_POLICY_CONFLICT = "A patient with this policy number already exists"
_RACE_CONFLICT = "Policy number is already in use or the user is already linked to another patient"


def _ensure_staff(user: User) -> None:
    """Список, создание и изменение пациентов - только admin / registrar."""
    if user.role not in STAFF_ROLES:
        raise PermissionDeniedError("Insufficient permissions to manage patients")


def _visibility_clause(actor: Actor) -> ColumnElement[bool] | None:
    """SQL-условие «какие пациенты пользователю видны»; ``None`` - ограничений нет."""
    if actor.role in STAFF_ROLES:
        return None
    if actor.role == UserRole.DOCTOR and actor.doctor_id is not None:
        return (
            select(Appointment.id)
            .where(
                Appointment.patient_id == Patient.id,
                Appointment.doctor_id == actor.doctor_id,
                Appointment.status != AppointmentStatus.CANCELLED,
            )
            .correlate(Patient)
            .exists()
        )
    if actor.role == UserRole.PATIENT and actor.patient_id is not None:
        return Patient.id == actor.patient_id
    return false()


async def list_patients(session: AsyncSession, user: User, q: str | None, *, limit: int, offset: int) -> list[Patient]:
    """Список пациентов; ``q`` - подстрока ФИО (без учёта регистра) или номера полиса.

    admin / registrar видят всех; врач - только своих пациентов (те же правила, что у
    ``get_patient``): так интерфейсу врача не нужно выгружать все записи и запрашивать
    пациентов по одному.
    """
    if user.role not in STAFF_ROLES and user.role != UserRole.DOCTOR:
        raise PermissionDeniedError("Insufficient permissions to list patients")
    stmt = select(Patient)
    clause = _visibility_clause(await load_actor(session, user))
    if clause is not None:
        stmt = stmt.where(clause)
    term = (q or "").strip()
    if term:
        stmt = stmt.where(
            or_(ci_contains(Patient.full_name, term), Patient.policy_number.contains(term, autoescape=True))
        )
    stmt = stmt.order_by(Patient.full_name, Patient.id).limit(limit).offset(offset)
    return list((await session.scalars(stmt)).all())


async def get_patient(session: AsyncSession, user: User, patient_id: int) -> Patient:
    """Пациент по id, если он виден пользователю; иначе 404."""
    actor = await load_actor(session, user)
    stmt = select(Patient).where(Patient.id == patient_id)
    clause = _visibility_clause(actor)
    if clause is not None:
        stmt = stmt.where(clause)
    patient = await session.scalar(stmt)
    if patient is None:
        raise NotFoundError("Patient not found")
    return patient


async def get_own_patient(session: AsyncSession, user: User) -> Patient:
    """Профиль пациента текущего пользователя (роль ``patient``); профиля нет - 404."""
    if user.role != UserRole.PATIENT:
        raise PermissionDeniedError("Only patients have a personal patient profile")
    patient = await session.scalar(select(Patient).where(Patient.user_id == user.id))
    if patient is None:
        raise NotFoundError("Patient profile not found")
    return patient


async def _check_user_link(session: AsyncSession, user_id: int, *, exclude_patient_id: int | None) -> None:
    """Пользователя можно привязать к пациенту: он есть, имеет роль patient и не занят другим пациентом."""
    user = await lock_user(session, user_id)  # блокировка сериализует привязку со сменой роли
    if user.role != UserRole.PATIENT:
        raise BusinessRuleError("The linked user must have the 'patient' role")
    stmt = select(Patient.id).where(Patient.user_id == user.id)
    if exclude_patient_id is not None:
        stmt = stmt.where(Patient.id != exclude_patient_id)
    if await session.scalar(stmt) is not None:
        raise ConflictError("The user is already linked to another patient")


async def create_patient(session: AsyncSession, user: User, data: PatientCreate) -> Patient:
    """Создаёт пациента (admin / registrar). Занятый полис - 409; ``user_id`` проверяется как при привязке."""
    _ensure_staff(user)
    if await session.scalar(select(Patient.id).where(Patient.policy_number == data.policy_number)) is not None:
        raise ConflictError(_POLICY_CONFLICT)
    if data.user_id is not None:
        await _check_user_link(session, data.user_id, exclude_patient_id=None)

    patient = Patient(
        user_id=data.user_id,
        full_name=data.full_name,
        birth_date=data.birth_date,
        phone=data.phone,
        policy_number=data.policy_number,
    )
    session.add(patient)
    await commit_or_conflict(session, _RACE_CONFLICT)
    return patient


async def update_patient(session: AsyncSession, user: User, patient_id: int, data: PatientUpdate) -> Patient:
    """PATCH пациента (admin / registrar), в том числе привязка и отвязка аккаунта.

    ``user_id: null`` отвязывает аккаунт, ``phone: null`` стирает телефон. Пустое тело - 422.
    """
    _ensure_staff(user)
    fields = data.model_fields_set
    if not fields:
        raise BusinessRuleError("No fields to update")

    stmt = select(Patient).where(Patient.id == patient_id).with_for_update().execution_options(populate_existing=True)
    patient = await session.scalar(stmt)
    if patient is None:
        raise NotFoundError("Patient not found")

    if data.policy_number is not None and data.policy_number != patient.policy_number:
        taken = await session.scalar(
            select(Patient.id).where(Patient.policy_number == data.policy_number, Patient.id != patient.id)
        )
        if taken is not None:
            raise ConflictError(_POLICY_CONFLICT)
        patient.policy_number = data.policy_number
    if "user_id" in fields:
        if data.user_id is not None and data.user_id != patient.user_id:
            await _check_user_link(session, data.user_id, exclude_patient_id=patient.id)
        patient.user_id = data.user_id
    if data.full_name is not None:
        patient.full_name = data.full_name
    if data.birth_date is not None:
        patient.birth_date = data.birth_date
    if "phone" in fields:
        patient.phone = data.phone

    await commit_or_conflict(session, _RACE_CONFLICT)
    return patient

"""Медицинские данные: назначения (рецепты) и записи медицинской карты.

Доступ строже, чем к расписанию:

* читать: врач - данные пациентов, у которых он вёл приём (у него есть запись в статусе
  ``completed``; правило уровня пациента, поэтому врач видит и то, что назначили другие
  врачи); пациент - только свои; admin и registrar - никогда (403: они работают с
  расписанием, а не с диагнозами);
* недоступный объект неотличим от несуществующего (404); 403 - административному персоналу
  и операциям, на которые у видимого объекта нет права (чужая запись медкарты);
* назначение создаёт только врач этой записи, запись должна быть ``completed``; изменения и
  удаления назначений нет (выписанный рецепт не меняется);
* запись медкарты создаёт только врач; ``doctor_id`` берётся из профиля текущего врача (если
  тело его содержит, значение должно совпасть); править её может только автор и только в течение
  24 часов после создания; удаления нет.

Слой владеет транзакцией (``commit()`` здесь). Время - aware UTC; «сейчас» приходит параметром.
"""

from datetime import datetime, timedelta

from sqlalchemy import ColumnElement, false, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute, aliased

from app.errors import BusinessRuleError, ConflictError, NotFoundError, PermissionDeniedError
from app.models import Appointment, AppointmentStatus, MedicalRecord, Patient, Prescription, User, UserRole
from app.schemas import MedicalRecordCreate, MedicalRecordUpdate, PrescriptionCreate
from app.services.appointments import STAFF_ROLES, Actor, load_actor

RECORD_EDIT_WINDOW = timedelta(hours=24)  # автор правит запись не позже чем через 24 часа (ровно 24 часа - можно)


def _ensure_not_staff(actor: Actor) -> None:
    """Административный персонал не читает медицинские данные (403)."""
    if actor.role in STAFF_ROLES:
        raise PermissionDeniedError("Medical data is not available to administrative staff")


def _require_doctor_id(actor: Actor) -> int:
    """id профиля врача для операций записи; не врач или нет профиля - 403."""
    if actor.role != UserRole.DOCTOR:
        raise PermissionDeniedError("Only doctors can perform this operation")
    if actor.doctor_id is None:
        raise PermissionDeniedError("Doctor profile not found")
    return actor.doctor_id


def _readable_patient_clause(actor: Actor, patient_column: InstrumentedAttribute[int]) -> ColumnElement[bool]:
    """SQL-условие «данные этого пациента пользователю доступны» для столбца ``patient_column``.

    Подзапрос строится на ``aliased(Appointment)``: иначе при внешнем запросе по ``appointments``
    SQLAlchemy автоматически «скоррелировал» бы подзапрос и убрал его FROM.
    """
    if actor.role == UserRole.PATIENT and actor.patient_id is not None:
        return patient_column == actor.patient_id
    if actor.role == UserRole.DOCTOR and actor.doctor_id is not None:
        treated = aliased(Appointment)
        return patient_column.in_(
            select(treated.patient_id).where(
                treated.doctor_id == actor.doctor_id,
                treated.status == AppointmentStatus.COMPLETED,
            )
        )
    return false()


async def _doctor_treated_patient(session: AsyncSession, doctor_id: int, patient_id: int) -> bool:
    """Вёл ли врач хотя бы один приём этого пациента (запись в статусе ``completed``)."""
    stmt = select(Appointment.id).where(
        Appointment.doctor_id == doctor_id,
        Appointment.patient_id == patient_id,
        Appointment.status == AppointmentStatus.COMPLETED,
    )
    return await session.scalar(stmt.limit(1)) is not None


# --------------------------------------------------------------------------- #
# Назначения (рецепты)
# --------------------------------------------------------------------------- #


async def create_prescription(
    session: AsyncSession, user: User, data: PrescriptionCreate, *, now: datetime
) -> Prescription:
    """Выписывает назначение: только врач этой записи (чужая запись - 404), запись ``completed`` (иначе 409)."""
    actor = await load_actor(session, user)
    doctor_id = _require_doctor_id(actor)

    appointment = await session.scalar(
        select(Appointment).where(Appointment.id == data.appointment_id, Appointment.doctor_id == doctor_id)
    )
    if appointment is None:
        raise NotFoundError("Appointment not found")
    if appointment.status != AppointmentStatus.COMPLETED:
        raise ConflictError("Prescriptions can only be issued for completed appointments")

    prescription = Prescription(
        appointment_id=appointment.id,
        medication=data.medication,
        dosage=data.dosage,
        instructions=data.instructions,
        issued_at=now,
    )
    session.add(prescription)
    await session.commit()
    return prescription


async def list_prescriptions(session: AsyncSession, user: User, appointment_id: int) -> list[Prescription]:
    """Назначения по записи, если данные её пациента пользователю доступны.

    admin / registrar - 403; недоступная или несуществующая запись - 404.
    """
    actor = await load_actor(session, user)
    _ensure_not_staff(actor)

    visible = await session.scalar(
        select(Appointment.id).where(
            Appointment.id == appointment_id,
            _readable_patient_clause(actor, Appointment.patient_id),
        )
    )
    if visible is None:
        raise NotFoundError("Appointment not found")

    stmt = (
        select(Prescription)
        .where(Prescription.appointment_id == appointment_id)
        .order_by(Prescription.issued_at, Prescription.id)
    )
    return list((await session.scalars(stmt)).all())


# --------------------------------------------------------------------------- #
# Медицинская карта
# --------------------------------------------------------------------------- #


async def create_medical_record(
    session: AsyncSession, user: User, data: MedicalRecordCreate, *, now: datetime
) -> MedicalRecord:
    """Создаёт запись медкарты от имени текущего врача.

    * ``data.doctor_id`` должен совпадать с профилем текущего врача, иначе 403;
    * указан ``appointment_id``: запись должна принадлежать этому врачу (иначе 404) и этому
      пациенту (иначе 422) и быть ``completed`` (иначе 409);
    * не указан: у врача должен быть хотя бы один ``completed``-приём этого пациента, иначе
      404 (пациент, которого врач не вёл, для него неотличим от несуществующего).
    """
    actor = await load_actor(session, user)
    doctor_id = _require_doctor_id(actor)
    if data.doctor_id != doctor_id:
        raise PermissionDeniedError("doctor_id must match the current doctor")

    if data.appointment_id is not None:
        appointment = await session.scalar(
            select(Appointment).where(Appointment.id == data.appointment_id, Appointment.doctor_id == doctor_id)
        )
        if appointment is None:
            raise NotFoundError("Appointment not found")
        if appointment.patient_id != data.patient_id:
            raise BusinessRuleError("The appointment belongs to a different patient")
        if appointment.status != AppointmentStatus.COMPLETED:
            raise ConflictError("Medical records can only be linked to completed appointments")
    elif not await _doctor_treated_patient(session, doctor_id, data.patient_id):
        raise NotFoundError("Patient not found")

    record = MedicalRecord(
        patient_id=data.patient_id,
        doctor_id=doctor_id,
        appointment_id=data.appointment_id,
        diagnosis_code=data.diagnosis_code,
        notes=data.notes,
        created_at=now,
    )
    session.add(record)
    await session.commit()
    return record


async def list_medical_records(
    session: AsyncSession, user: User, patient_id: int, *, limit: int, offset: int
) -> list[MedicalRecord]:
    """Записи медкарты пациента, новые первыми (``created_at`` по убыванию, затем ``id``).

    admin / registrar - 403; недоступный или несуществующий пациент - 404.
    """
    actor = await load_actor(session, user)
    _ensure_not_staff(actor)

    visible = await session.scalar(
        select(Patient.id).where(Patient.id == patient_id, _readable_patient_clause(actor, Patient.id))
    )
    if visible is None:
        raise NotFoundError("Patient not found")

    stmt = (
        select(MedicalRecord)
        .where(MedicalRecord.patient_id == patient_id)
        .order_by(MedicalRecord.created_at.desc(), MedicalRecord.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list((await session.scalars(stmt)).all())


async def update_medical_record(
    session: AsyncSession, user: User, record_id: int, data: MedicalRecordUpdate, *, now: datetime
) -> MedicalRecord:
    """PATCH записи медкарты: только автор и только в течение 24 часов после создания.

    * не врач - 403; пустое тело - 422;
    * записи нет или пациент врачу недоступен - 404;
    * запись видна врачу, но написана не им - 403;
    * прошло больше 24 часов - 403 (как окно пациента в записях: право истекло по времени).
    """
    actor = await load_actor(session, user)
    doctor_id = _require_doctor_id(actor)
    fields = data.model_fields_set
    if not fields:
        raise BusinessRuleError("No fields to update")

    stmt = select(MedicalRecord).where(MedicalRecord.id == record_id).with_for_update()
    record = await session.scalar(stmt.execution_options(populate_existing=True))
    if record is None:
        raise NotFoundError("Medical record not found")
    if record.doctor_id != doctor_id:
        if await _doctor_treated_patient(session, doctor_id, record.patient_id):
            raise PermissionDeniedError("Only the author can modify a medical record")
        raise NotFoundError("Medical record not found")
    if now - record.created_at > RECORD_EDIT_WINDOW:
        raise PermissionDeniedError("A medical record can only be modified within 24 hours after creation")

    if "diagnosis_code" in fields:
        record.diagnosis_code = data.diagnosis_code  # type: ignore[assignment]  # None запрещён схемой
    if "notes" in fields:
        record.notes = data.notes
    await session.commit()
    return record

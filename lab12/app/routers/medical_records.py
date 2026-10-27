"""Роуты медицинской карты: создать запись, прочитать карту пациента, поправить свою запись.

Удаления нет. admin и registrar получают 403. ``doctor_id`` в теле обязателен по схеме
``MedicalRecordCreate``, но значимо только его совпадение с профилем текущего врача.
"""

from fastapi import APIRouter, status

from app.models import MedicalRecord
from app.routers.common import DoctorUser, IdPath, LimitQuery, MedicalReaderUser, NowDep, OffsetQuery, SessionDep
from app.schemas import MedicalRecordCreate, MedicalRecordOut, MedicalRecordUpdate
from app.services import medical as service

router = APIRouter(prefix="/api", tags=["medical-records"])


@router.post("/medical-records", response_model=MedicalRecordOut, status_code=status.HTTP_201_CREATED)
async def create_medical_record(
    data: MedicalRecordCreate, user: DoctorUser, session: SessionDep, now: NowDep
) -> MedicalRecord:
    """Создаёт запись медкарты от имени текущего врача."""
    return await service.create_medical_record(session, user, data, now=now)


@router.get("/patients/{patient_id}/medical-records", response_model=list[MedicalRecordOut])
async def list_medical_records(
    patient_id: IdPath,
    user: MedicalReaderUser,
    session: SessionDep,
    limit: LimitQuery = 50,
    offset: OffsetQuery = 0,
) -> list[MedicalRecord]:
    """Медкарта пациента, новые записи первыми."""
    return await service.list_medical_records(session, user, patient_id, limit=limit, offset=offset)


@router.patch("/medical-records/{record_id}", response_model=MedicalRecordOut)
async def update_medical_record(
    record_id: IdPath, data: MedicalRecordUpdate, user: DoctorUser, session: SessionDep, now: NowDep
) -> MedicalRecord:
    """Правка записи: только автор и только в течение 24 часов после создания."""
    return await service.update_medical_record(session, user, record_id, data, now=now)

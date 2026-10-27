"""Роуты назначений (рецептов): выписать и посмотреть по записи.

Редактирования и удаления нет: выписанный рецепт не меняется. admin и registrar получают 403
на обоих эндпоинтах (медицинские данные), чужая запись для врача/пациента - 404.
"""

from fastapi import APIRouter, status

from app.models import Prescription
from app.routers.common import DoctorUser, IdPath, MedicalReaderUser, NowDep, SessionDep
from app.schemas import PrescriptionCreate, PrescriptionOut
from app.services import medical as service

router = APIRouter(prefix="/api", tags=["prescriptions"])


@router.post("/prescriptions", response_model=PrescriptionOut, status_code=status.HTTP_201_CREATED)
async def create_prescription(
    data: PrescriptionCreate, user: DoctorUser, session: SessionDep, now: NowDep
) -> Prescription:
    """Выписывает назначение: врач этой записи, запись в статусе completed."""
    return await service.create_prescription(session, user, data, now=now)


@router.get("/appointments/{appointment_id}/prescriptions", response_model=list[PrescriptionOut])
async def list_prescriptions(
    appointment_id: IdPath, user: MedicalReaderUser, session: SessionDep
) -> list[Prescription]:
    """Назначения по записи (врач, который вёл пациента; сам пациент)."""
    return await service.list_prescriptions(session, user, appointment_id)

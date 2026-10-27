"""Роуты пациентов.

``GET /patients/me`` объявлен раньше ``GET /patients/{patient_id}``: иначе "me" попал бы
в числовой параметр пути и дал 422. Грубая фильтрация по ролям - через ``require_roles``;
«врач видит только своих пациентов» и «пациент - только себя» проверяет сервис (404).
"""

from fastapi import APIRouter, status

from app.models import Patient
from app.routers.common import (
    IdPath,
    LimitQuery,
    OffsetQuery,
    PatientUser,
    SearchQuery,
    SessionDep,
    StaffUser,
)
from app.schemas import PatientCreate, PatientOut, PatientUpdate
from app.security import CurrentUser
from app.services import patients as service

router = APIRouter(prefix="/api", tags=["patients"])


@router.get("/patients", response_model=list[PatientOut])
async def list_patients(
    user: CurrentUser,
    session: SessionDep,
    q: SearchQuery = None,
    limit: LimitQuery = 20,
    offset: OffsetQuery = 0,
) -> list[Patient]:
    """Поиск пациентов: admin, registrar - все; врач - только свои. ``q`` - подстрока ФИО или номера полиса."""
    return await service.list_patients(session, user, q, limit=limit, offset=offset)


@router.get("/patients/me", response_model=PatientOut)
async def get_my_patient(user: PatientUser, session: SessionDep) -> Patient:
    """Собственный профиль пациента."""
    return await service.get_own_patient(session, user)


@router.get("/patients/{patient_id}", response_model=PatientOut)
async def get_patient(patient_id: IdPath, user: CurrentUser, session: SessionDep) -> Patient:
    """Пациент по id: admin / registrar - любой; врач - только свой по записям; пациент - только себя."""
    return await service.get_patient(session, user, patient_id)


@router.post("/patients", response_model=PatientOut, status_code=status.HTTP_201_CREATED)
async def create_patient(data: PatientCreate, user: StaffUser, session: SessionDep) -> Patient:
    """Создаёт пациента (admin, registrar)."""
    return await service.create_patient(session, user, data)


@router.patch("/patients/{patient_id}", response_model=PatientOut)
async def update_patient(patient_id: IdPath, data: PatientUpdate, user: StaffUser, session: SessionDep) -> Patient:
    """Изменяет пациента, в том числе привязывает / отвязывает аккаунт (admin, registrar)."""
    return await service.update_patient(session, user, patient_id, data)

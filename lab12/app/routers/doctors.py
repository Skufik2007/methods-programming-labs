"""Роуты врачей: чтение - всем аутентифицированным, создание и изменение - администратору.

Ответы содержат ФИО врача (``DoctorDetailOut``). Сборка ответа из ORM-объекта и ФИО
выполняется здесь, чтобы не трогать связи ``lazy="raise"``.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.routers.common import AdminUser, IdPath, NowDep, SessionDep
from app.schemas import DoctorCreate, DoctorUpdate
from app.schemas_extra import DoctorDetailOut
from app.security import get_current_user
from app.services import doctors as service
from app.services.doctors import DoctorView

router = APIRouter(prefix="/api", tags=["doctors"])


def _to_out(view: DoctorView) -> DoctorDetailOut:
    return DoctorDetailOut.from_doctor(view.doctor, view.full_name)


@router.get(
    "/doctors",
    response_model=list[DoctorDetailOut],
    dependencies=[Depends(get_current_user)],  # любой аутентифицированный пользователь
)
async def list_doctors(
    session: SessionDep,
    specialty: Annotated[str | None, Query(min_length=1, max_length=100)] = None,
) -> list[DoctorDetailOut]:
    """Врачи с активным аккаунтом; ``specialty`` - точное совпадение без учёта регистра."""
    return [_to_out(view) for view in await service.list_doctors(session, specialty=specialty)]


@router.get(
    "/doctors/{doctor_id}",
    response_model=DoctorDetailOut,
    dependencies=[Depends(get_current_user)],
)
async def get_doctor(doctor_id: IdPath, session: SessionDep) -> DoctorDetailOut:
    """Один врач (в том числе деактивированный, если знать id)."""
    return _to_out(await service.get_doctor(session, doctor_id))


@router.post("/doctors", response_model=DoctorDetailOut, status_code=status.HTTP_201_CREATED)
async def create_doctor(data: DoctorCreate, user: AdminUser, session: SessionDep) -> DoctorDetailOut:
    """Профиль врача для пользователя с ролью doctor, у которого профиля ещё нет (admin)."""
    return _to_out(await service.create_doctor(session, user, data))


@router.patch("/doctors/{doctor_id}", response_model=DoctorDetailOut)
async def update_doctor(
    doctor_id: IdPath, data: DoctorUpdate, user: AdminUser, session: SessionDep, now: NowDep
) -> DoctorDetailOut:
    """Кабинет, специальность, график (admin). Конфликт с будущими записями - 409."""
    return _to_out(await service.update_doctor(session, user, doctor_id, data, now=now))

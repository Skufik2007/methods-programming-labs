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

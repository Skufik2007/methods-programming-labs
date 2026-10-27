"""Роуты аналитики (admin и registrar): агрегаты без персональных и медицинских данных людей.

``date_from`` / ``date_to`` обязательны, это местные даты клиники, обе границы включительно;
период длиннее 366 дней - 422 (проверяет сервис).
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.models import UserRole
from app.routers.common import RequiredDate, SessionDep
from app.schemas_extra import AppointmentsSummaryOut, DayCountOut, DiagnosisCountOut, DoctorLoadOut
from app.security import require_roles
from app.services import analytics as service

router = APIRouter(
    prefix="/api/analytics",
    tags=["analytics"],
    dependencies=[Depends(require_roles(UserRole.ADMIN, UserRole.REGISTRAR))],
)


@router.get("/summary", response_model=AppointmentsSummaryOut)
async def get_summary(session: SessionDep, date_from: RequiredDate, date_to: RequiredDate) -> AppointmentsSummaryOut:
    """Записи по статусам, доля неявок и доля отмен."""
    return await service.summary(session, date_from, date_to)


@router.get("/doctors-load", response_model=list[DoctorLoadOut])
async def get_doctors_load(session: SessionDep, date_from: RequiredDate, date_to: RequiredDate) -> list[DoctorLoadOut]:
    """Доступные и занятые минуты, загрузка в процентах, completed и no_show по каждому врачу."""
    return await service.doctors_load(session, date_from, date_to)


@router.get("/appointments-per-day", response_model=list[DayCountOut])
async def get_appointments_per_day(
    session: SessionDep, date_from: RequiredDate, date_to: RequiredDate
) -> list[DayCountOut]:
    """Число записей по местным датам клиники (дни без записей - с нулём)."""
    return await service.appointments_per_day(session, date_from, date_to)


@router.get("/top-diagnoses", response_model=list[DiagnosisCountOut])
async def get_top_diagnoses(
    session: SessionDep,
    date_from: RequiredDate,
    date_to: RequiredDate,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> list[DiagnosisCountOut]:
    """Самые частые коды МКБ-10 (только код и количество)."""
    return await service.top_diagnoses(session, date_from, date_to, limit)

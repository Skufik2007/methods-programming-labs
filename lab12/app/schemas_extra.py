"""Дополнительные схемы ответов: врач с ФИО и аналитика.

Схемы аналитики содержат только агрегаты: ни идентификаторов пациентов, ни ФИО, ни текстов
медицинских записей (из диагноза наружу выходит только код МКБ-10 и число случаев).
Входные схемы и «готовые» Out-схемы живут в ``app/schemas.py`` и здесь не дублируются.
"""

from datetime import date
from typing import Self

from pydantic import BaseModel, Field

from app.models import Doctor
from app.schemas import DoctorOut


class DoctorDetailOut(DoctorOut):
    """Врач вместе с ФИО (ФИО хранится у пользователя, а не у профиля врача)."""

    full_name: str

    @classmethod
    def from_doctor(cls, doctor: Doctor, full_name: str) -> Self:
        """Собирает ответ из ORM-врача и ФИО; связи ORM (``lazy="raise"``) не читаются."""
        return cls(**DoctorOut.model_validate(doctor).model_dump(), full_name=full_name)


# --------------------------------------------------------------------------- #
# Аналитика
# --------------------------------------------------------------------------- #


class StatusCountsOut(BaseModel):
    """Число записей по статусам; отсутствующие статусы - 0."""

    scheduled: int = 0
    completed: int = 0
    cancelled: int = 0
    no_show: int = 0


class AppointmentsSummaryOut(BaseModel):
    """Сводка по записям за период (местные даты клиники, обе границы включительно)."""

    date_from: date
    date_to: date
    total: int
    by_status: StatusCountsOut
    no_show_rate: float = Field(
        description="no_show / (completed + no_show), 0..1; 0.0, если завершённых приёмов не было"
    )
    cancellation_rate: float = Field(description="cancelled / total, 0..1; 0.0, если записей не было")


class DoctorLoadOut(BaseModel):
    """Загрузка одного врача за период. ФИО намеренно нет: имя доступно через ``GET /api/doctors``."""

    doctor_id: int
    specialty: str
    is_active: bool
    available_minutes: int = Field(description="Сумма длительностей смен по рабочим дням графика")
    busy_minutes: int = Field(description="Суммарная длительность записей, кроме cancelled")
    load_percent: float = Field(
        description="busy_minutes / available_minutes * 100; не ограничена сверху; 0.0 при нулевом графике"
    )
    completed: int
    no_show: int


class DayCountOut(BaseModel):
    """Число записей на местную дату клиники (все статусы)."""

    day: date
    count: int


class DiagnosisCountOut(BaseModel):
    """Код МКБ-10 и число записей медкарты с этим кодом. Больше ничего."""

    code: str
    count: int

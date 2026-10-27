"""Аналитика для администратора и регистратора: агрегаты без персональных и медицинских данных людей.

* Даты параметров - местные даты клиники, обе границы включительно; период не длиннее
  ``MAX_PERIOD_DAYS`` дней (иначе 422). Записи относятся к периоду по ``starts_at``, записи
  медкарты (диагнозы) - по ``created_at``; границы периода переводятся в UTC как
  полуоткрытый интервал ``[00:00 первого дня, 00:00 после последнего)``.
* Подсчёт - агрегатами в SQL (``GROUP BY``), в Python приходят только готовые строки-итоги.
* Доступные минуты врача по графику считает чистая функция ``available_minutes`` (без БД).

Две вычисляемые величины зависят от СУБД (в проекте допустимы PostgreSQL и SQLite для тестов):
длительность записи в секундах и местная дата записи. Для PostgreSQL используются
``EXTRACT(EPOCH ...)`` и ``timezone(...)`` (точно, с учётом перевода часов); для SQLite -
``julianday`` и сдвиг ``date(..., '+N seconds')`` по отрезкам постоянного смещения UTC.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import ColumnElement, Date, case, cast, extract, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.errors import BusinessRuleError
from app.models import Appointment, AppointmentStatus, Doctor, MedicalRecord, User
from app.schemas_extra import (
    AppointmentsSummaryOut,
    DayCountOut,
    DiagnosisCountOut,
    DoctorLoadOut,
    StatusCountsOut,
)
from app.services import scheduling
from app.services.appointments import _schedule_of  # noqa: PLC2701 - единое отображение Doctor -> Schedule

MAX_PERIOD_DAYS = 366


@dataclass(frozen=True, slots=True)
class Period:
    """Проверенный период: местные даты и соответствующий полуоткрытый интервал ``[start, end)`` в UTC."""

    date_from: date
    date_to: date
    start: datetime
    end: datetime

    @property
    def days(self) -> int:
        """Число местных дней в периоде (обе границы включительно)."""
        return (self.date_to - self.date_from).days + 1


def make_period(date_from: date, date_to: date, tz: ZoneInfo) -> Period:
    """Проверяет период и переводит его в UTC. ``date_from > date_to`` и период длиннее 366 дней - 422."""
    if date_from > date_to:
        raise BusinessRuleError("date_from must not be later than date_to")
    if (date_to - date_from).days + 1 > MAX_PERIOD_DAYS:
        raise BusinessRuleError(f"The period must not be longer than {MAX_PERIOD_DAYS} days")
    start, _ = scheduling.local_day_bounds(date_from, tz)
    _, end = scheduling.local_day_bounds(date_to, tz)
    return Period(date_from=date_from, date_to=date_to, start=start, end=end)


def _period(date_from: date, date_to: date) -> Period:
    return make_period(date_from, date_to, get_settings().clinic_tz)


def _iter_days(date_from: date, date_to: date) -> Iterator[date]:
    for offset in range((date_to - date_from).days + 1):
        yield date_from + timedelta(days=offset)


def available_minutes(schedule: scheduling.Schedule, date_from: date, date_to: date) -> int:
    """Доступные минуты врача по графику за период ``[date_from, date_to]`` (местные даты, включительно).

    Это длина смены (``work_end - work_start``) в минутах, умноженная на число дней периода,
    чей ISO-день недели входит в ``schedule.work_days``. Чистая функция: без БД, без часов,
    без часового пояса (даты уже местные). Дни считаются без цикла по датам: полные недели
    дают по одному вхождению каждого дня недели, остаток (до 6 дней) досчитывается явно.

    Raises:
        ValueError: ``date_from`` позже ``date_to``.
    """
    if date_from > date_to:
        raise ValueError("date_from must not be later than date_to")
    shift = datetime.combine(date_from, schedule.work_end) - datetime.combine(date_from, schedule.work_start)
    shift_minutes = int(shift.total_seconds() // 60)

    full_weeks, rest = divmod((date_to - date_from).days + 1, 7)
    working_days = full_weeks * len(schedule.work_days)
    first_weekday = date_from.isoweekday()  # остаток начинается с того же дня недели, что и период
    working_days += sum(1 for step in range(rest) if (first_weekday - 1 + step) % 7 + 1 in schedule.work_days)
    return working_days * shift_minutes


def _ratio(numerator: int, denominator: int) -> float:
    """Доля с округлением до 4 знаков; при нулевом знаменателе - 0.0."""
    return round(numerator / denominator, 4) if denominator else 0.0


def _in_period(period: Period) -> tuple[ColumnElement[bool], ColumnElement[bool]]:
    return Appointment.starts_at >= period.start, Appointment.starts_at < period.end


# --------------------------------------------------------------------------- #
# Сводка по статусам
# --------------------------------------------------------------------------- #


async def summary(session: AsyncSession, date_from: date, date_to: date) -> AppointmentsSummaryOut:
    """Число записей по статусам, доля неявок и доля отмен за период."""
    period = _period(date_from, date_to)
    stmt = select(Appointment.status, func.count()).where(*_in_period(period)).group_by(Appointment.status)
    counts = {status: 0 for status in AppointmentStatus}
    for status, number in (await session.execute(stmt)).all():
        counts[AppointmentStatus(status)] = int(number)

    completed = counts[AppointmentStatus.COMPLETED]
    no_show = counts[AppointmentStatus.NO_SHOW]
    cancelled = counts[AppointmentStatus.CANCELLED]
    total = sum(counts.values())
    return AppointmentsSummaryOut(
        date_from=period.date_from,
        date_to=period.date_to,
        total=total,
        by_status=StatusCountsOut(
            scheduled=counts[AppointmentStatus.SCHEDULED],
            completed=completed,
            cancelled=cancelled,
            no_show=no_show,
        ),
        no_show_rate=_ratio(no_show, completed + no_show),
        cancellation_rate=_ratio(cancelled, total),
    )


# --------------------------------------------------------------------------- #
# Загрузка врачей
# --------------------------------------------------------------------------- #


def _duration_seconds(session: AsyncSession) -> ColumnElement[Any]:
    """SQL-выражение длительности записи в секундах (зависит от СУБД)."""
    if session.bind.dialect.name == "sqlite":
        return func.round((func.julianday(Appointment.ends_at) - func.julianday(Appointment.starts_at)) * 86400)
    return extract("epoch", Appointment.ends_at - Appointment.starts_at)


async def doctors_load(session: AsyncSession, date_from: date, date_to: date) -> list[DoctorLoadOut]:
    """Загрузка каждого врача за период (два запроса независимо от числа врачей).

    Занятые минуты - длительность записей кроме ``cancelled``. Загрузка в процентах не
    ограничена сверху (записи, созданные до сужения графика, могут давать больше 100) и
    равна 0.0, если по графику доступных минут нет. Деактивированные врачи включены с
    ``is_active=false``.
    """
    period = _period(date_from, date_to)

    doctor_rows = await session.execute(
        select(Doctor, User.is_active).select_from(Doctor).join(User, User.id == Doctor.user_id).order_by(Doctor.id)
    )
    stmt = (
        select(
            Appointment.doctor_id,
            func.sum(
                case((Appointment.status != AppointmentStatus.CANCELLED, _duration_seconds(session)), else_=0)
            ).label("busy_seconds"),
            func.sum(case((Appointment.status == AppointmentStatus.COMPLETED, 1), else_=0)).label("completed"),
            func.sum(case((Appointment.status == AppointmentStatus.NO_SHOW, 1), else_=0)).label("no_show"),
        )
        .where(*_in_period(period))
        .group_by(Appointment.doctor_id)
    )
    stats = {
        doctor_id: (float(busy or 0), int(completed or 0), int(no_show or 0))
        for doctor_id, busy, completed, no_show in (await session.execute(stmt)).all()
    }

    result: list[DoctorLoadOut] = []
    for doctor, is_active in doctor_rows.all():
        busy_seconds, completed, no_show = stats.get(doctor.id, (0.0, 0, 0))
        available = available_minutes(_schedule_of(doctor), period.date_from, period.date_to)
        busy = int(round(busy_seconds / 60))
        result.append(
            DoctorLoadOut(
                doctor_id=doctor.id,
                specialty=doctor.specialty,
                is_active=bool(is_active),
                available_minutes=available,
                busy_minutes=busy,
                load_percent=round(busy * 100 / available, 2) if available else 0.0,
                completed=completed,
                no_show=no_show,
            )
        )
    return result


# --------------------------------------------------------------------------- #
# Записи по дням
# --------------------------------------------------------------------------- #


def _utc_offset_segments(period: Period, tz: ZoneInfo) -> list[tuple[datetime, int]]:
    """Отрезки периода с постоянным смещением местного времени от UTC: ``[(начало в UTC, секунды)]``.

    Смещение берётся на местную полночь каждого дня; подряд идущие дни с одним смещением
    сливаются. Для часового пояса без перевода часов (Europe/Moscow) отрезок один.
    """
    segments: list[tuple[datetime, int]] = []
    for day in _iter_days(period.date_from, period.date_to):
        start_utc, _ = scheduling.local_day_bounds(day, tz)
        offset = int((datetime.combine(day, time.min) - start_utc.replace(tzinfo=None)).total_seconds())
        if not segments or segments[-1][1] != offset:
            segments.append((start_utc, offset))
    return segments


def _local_date_expression(session: AsyncSession, period: Period, tz: ZoneInfo) -> ColumnElement[Any]:
    """SQL-выражение «местная дата начала записи» (зависит от СУБД, см. docstring модуля)."""
    if session.bind.dialect.name != "sqlite":
        return cast(func.timezone(get_settings().clinic_timezone, Appointment.starts_at), Date)

    def shifted(offset: int) -> ColumnElement[Any]:
        return func.date(Appointment.starts_at, f"{offset:+d} seconds")

    segments = _utc_offset_segments(period, tz)
    if len(segments) == 1:
        return shifted(segments[0][1])
    # Последний подходящий отрезок выигрывает, поэтому WHEN идут от поздних к ранним.
    return case(*[(Appointment.starts_at >= start, shifted(offset)) for start, offset in reversed(segments)])


def _as_date(value: Any) -> date:
    """Местная дата из результата SQL: PostgreSQL отдаёт ``date``, SQLite - строку ``YYYY-MM-DD``."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


async def appointments_per_day(session: AsyncSession, date_from: date, date_to: date) -> list[DayCountOut]:
    """Число записей (всех статусов) по местным датам клиники; дни без записей - с нулём.

    Группировка идёт по подзапросу с вычисленной датой: выражение с bind-параметрами не
    повторяется в ``SELECT`` и ``GROUP BY`` (PostgreSQL с asyncpg счёл бы их разными).
    """
    period = _period(date_from, date_to)
    local_day = _local_date_expression(session, period, get_settings().clinic_tz).label("day")
    inner = select(local_day).where(*_in_period(period)).subquery()
    stmt = select(inner.c.day, func.count()).group_by(inner.c.day)
    counts = {_as_date(day): int(number) for day, number in (await session.execute(stmt)).all()}
    return [DayCountOut(day=day, count=counts.get(day, 0)) for day in _iter_days(period.date_from, period.date_to)]


# --------------------------------------------------------------------------- #
# Частые диагнозы
# --------------------------------------------------------------------------- #


async def top_diagnoses(session: AsyncSession, date_from: date, date_to: date, limit: int) -> list[DiagnosisCountOut]:
    """Самые частые коды МКБ-10 среди записей медкарты, созданных за период.

    Наружу уходят только код и число; при равенстве чисел порядок по коду.
    """
    period = _period(date_from, date_to)
    number = func.count().label("number")
    stmt = (
        select(MedicalRecord.diagnosis_code, number)
        .where(MedicalRecord.created_at >= period.start, MedicalRecord.created_at < period.end)
        .group_by(MedicalRecord.diagnosis_code)
        .order_by(number.desc(), MedicalRecord.diagnosis_code)
        .limit(limit)
    )
    return [DiagnosisCountOut(code=code, count=int(count)) for code, count in (await session.execute(stmt)).all()]

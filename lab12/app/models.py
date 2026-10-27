"""ORM-модели системы управления клиникой (SQLAlchemy 2.0: Mapped / mapped_column).

Соглашения:

* все моменты времени хранятся и возвращаются как timezone-aware UTC (см. ``UTCDateTime``);
* ``Doctor.work_start`` / ``work_end`` - «настенное» время клиники (CLINIC_TIMEZONE) без смещения;
* внешние ключи ``ON DELETE RESTRICT``: медицинские данные не удаляются каскадно,
  из оборота их выводят флагом ``is_active`` или статусом записи;
* связи объявлены с ``lazy="raise"``: в async-коде данные нужно подгружать явно
  (``selectinload`` / ``joinedload``), иначе неявная ленивая загрузка дала бы ``MissingGreenlet``.
"""

import enum
from datetime import UTC, date, datetime, time
from typing import Optional

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    SmallInteger,
    String,
    Text,
    Time,
    func,
    text,
    true,
)
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Базовый класс моделей. ``Base.metadata`` используется Alembic-миграциями."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def utc_now() -> datetime:
    """Текущий момент как timezone-aware datetime в UTC."""
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """``TIMESTAMP WITH TIME ZONE``, гарантирующий aware-UTC на записи и чтении.

    * naive-значения при записи отклоняются (``ValueError``), а не трактуются неявно;
    * aware-значения приводятся к UTC перед записью;
    * SQLite (aiosqlite) теряет часовой пояс и отдаёт naive datetime - при чтении
      ему возвращается ``tzinfo=UTC``. В PostgreSQL значение уже aware.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("naive datetime is not allowed: use timezone-aware datetime")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class UserRole(enum.StrEnum):
    """Роли пользователей системы."""

    ADMIN = "admin"
    REGISTRAR = "registrar"
    DOCTOR = "doctor"
    PATIENT = "patient"


class AppointmentStatus(enum.StrEnum):
    """Статусы записи на приём."""

    SCHEDULED = "scheduled"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    NO_SHOW = "no_show"


def _enum_type(enum_cls: type[enum.Enum], name: str) -> Enum:
    """Enum, хранимый как VARCHAR + CHECK (без нативных enum PostgreSQL), по *значениям*.

    Нативный enum усложняет миграции; CHECK даёт ту же защиту и одинаково работает
    в PostgreSQL и SQLite. ``values_callable`` нужен, чтобы в БД попадали значения
    (``cancelled``), а не имена членов (``CANCELLED``) - на них опирается частичный индекс.
    """
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        length=16,
        create_constraint=True,
        validate_strings=True,
        values_callable=lambda members: [member.value for member in members],
    )


def _default_work_days() -> list[int]:
    """Рабочие дни по умолчанию: понедельник - пятница (ISO: 1 = понедельник)."""
    return [1, 2, 3, 4, 5]


class User(Base):
    """Учётная запись пользователя."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True)
    password_hash: Mapped[str] = mapped_column(String(128))
    full_name: Mapped[str] = mapped_column(String(255))
    role: Mapped[UserRole] = mapped_column(_enum_type(UserRole, "user_role"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    # Увеличивается при смене пароля: токены, выданные раньше, перестают приниматься.
    token_version: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, server_default=func.now())

    doctor_profile: Mapped[Optional["Doctor"]] = relationship(back_populates="user", lazy="raise")
    patient_profile: Mapped[Optional["Patient"]] = relationship(back_populates="user", lazy="raise")


class Doctor(Base):
    """Профиль врача (1:1 с пользователем) и его график работы."""

    __tablename__ = "doctors"
    __table_args__ = (
        CheckConstraint("slot_minutes BETWEEN 5 AND 240", name="slot_minutes_range"),
        CheckConstraint("work_start < work_end", name="shift_order"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), unique=True)
    specialty: Mapped[str] = mapped_column(String(100))
    cabinet: Mapped[str] = mapped_column(String(20))
    slot_minutes: Mapped[int] = mapped_column(SmallInteger, default=30, server_default=text("30"))
    work_start: Mapped[time] = mapped_column(Time)
    work_end: Mapped[time] = mapped_column(Time)
    # ISO-номера дней недели: 1 = понедельник ... 7 = воскресенье. Список нужно
    # присваивать целиком (изменения «на месте» ORM не отслеживает).
    work_days: Mapped[list[int]] = mapped_column(JSON, default=_default_work_days)

    user: Mapped["User"] = relationship(back_populates="doctor_profile", lazy="raise")
    appointments: Mapped[list["Appointment"]] = relationship(back_populates="doctor", lazy="raise")
    medical_records: Mapped[list["MedicalRecord"]] = relationship(back_populates="doctor", lazy="raise")


class Patient(Base):
    """Пациент. Аккаунт (``user_id``) необязателен: пациента заводит регистратор."""

    __tablename__ = "patients"
    __table_args__ = (
        # Портативная проверка длины; что все 16 символов - цифры, проверяет Pydantic-схема.
        CheckConstraint("length(policy_number) = 16", name="policy_number_length"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), unique=True)
    full_name: Mapped[str] = mapped_column(String(255))
    birth_date: Mapped[date] = mapped_column(Date)
    phone: Mapped[str | None] = mapped_column(String(16))  # E.164: "+" и до 15 цифр
    policy_number: Mapped[str] = mapped_column(String(16), unique=True)

    user: Mapped[Optional["User"]] = relationship(back_populates="patient_profile", lazy="raise")
    appointments: Mapped[list["Appointment"]] = relationship(back_populates="patient", lazy="raise")
    medical_records: Mapped[list["MedicalRecord"]] = relationship(back_populates="patient", lazy="raise")


# Предикат частичного индекса: «неотменённая» запись.
_ACTIVE_APPOINTMENT = text(f"status <> '{AppointmentStatus.CANCELLED.value}'")


class Appointment(Base):
    """Запись пациента на приём к врачу."""

    __tablename__ = "appointments"
    __table_args__ = (
        CheckConstraint("ends_at > starts_at", name="ends_after_starts"),
        # Врач не может иметь две неотменённые записи на одно время.
        Index(
            "uq_appointments_doctor_id_starts_at_active",
            "doctor_id",
            "starts_at",
            unique=True,
            postgresql_where=_ACTIVE_APPOINTMENT,
            sqlite_where=_ACTIVE_APPOINTMENT,
        ),
        # Индексы по полям фильтрации.
        Index("ix_appointments_doctor_id_starts_at", "doctor_id", "starts_at"),
        Index("ix_appointments_patient_id", "patient_id"),
        Index("ix_appointments_status", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    doctor_id: Mapped[int] = mapped_column(ForeignKey("doctors.id", ondelete="RESTRICT"))
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id", ondelete="RESTRICT"))
    starts_at: Mapped[datetime] = mapped_column(UTCDateTime)
    ends_at: Mapped[datetime] = mapped_column(UTCDateTime)
    status: Mapped[AppointmentStatus] = mapped_column(
        _enum_type(AppointmentStatus, "appointment_status"),
        default=AppointmentStatus.SCHEDULED,
        server_default=AppointmentStatus.SCHEDULED.value,
    )
    reason: Mapped[str | None] = mapped_column(String(500))
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, server_default=func.now())

    doctor: Mapped["Doctor"] = relationship(back_populates="appointments", lazy="raise")
    patient: Mapped["Patient"] = relationship(back_populates="appointments", lazy="raise")
    prescriptions: Mapped[list["Prescription"]] = relationship(back_populates="appointment", lazy="raise")


class Prescription(Base):
    """Назначение (рецепт), выданное в рамках приёма."""

    __tablename__ = "prescriptions"
    __table_args__ = (Index("ix_prescriptions_appointment_id", "appointment_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    appointment_id: Mapped[int] = mapped_column(ForeignKey("appointments.id", ondelete="RESTRICT"))
    medication: Mapped[str] = mapped_column(String(255))
    dosage: Mapped[str] = mapped_column(String(100))
    instructions: Mapped[str | None] = mapped_column(Text)
    issued_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, server_default=func.now())

    appointment: Mapped["Appointment"] = relationship(back_populates="prescriptions", lazy="raise")


class MedicalRecord(Base):
    """Запись в медицинской карте пациента (диагноз по МКБ-10 и заметки врача)."""

    __tablename__ = "medical_records"
    __table_args__ = (
        # Портативная проверка длины ("J06" ... "J06.99"); формат кода проверяет Pydantic-схема.
        CheckConstraint("length(diagnosis_code) BETWEEN 3 AND 6", name="diagnosis_code_length"),
        Index("ix_medical_records_patient_id", "patient_id"),
        Index("ix_medical_records_doctor_id", "doctor_id"),
        Index("ix_medical_records_appointment_id", "appointment_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id", ondelete="RESTRICT"))
    doctor_id: Mapped[int] = mapped_column(ForeignKey("doctors.id", ondelete="RESTRICT"))
    appointment_id: Mapped[int | None] = mapped_column(ForeignKey("appointments.id", ondelete="RESTRICT"))
    diagnosis_code: Mapped[str] = mapped_column(String(6))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, server_default=func.now())

    patient: Mapped["Patient"] = relationship(back_populates="medical_records", lazy="raise")
    doctor: Mapped["Doctor"] = relationship(back_populates="medical_records", lazy="raise")
    appointment: Mapped[Optional["Appointment"]] = relationship(lazy="raise")

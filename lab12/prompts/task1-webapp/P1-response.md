### app/config.py

```python
"""Настройки приложения из переменных окружения (и необязательного файла ``.env``).

``DATABASE_URL`` и ``JWT_SECRET`` не имеют значений по умолчанию: если любая из
них не задана или некорректна, ``get_settings()`` бросает ``pydantic.ValidationError``,
и приложение не стартует. Секретов в коде нет.
"""

from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.exc import ArgumentError
from sqlalchemy.engine import make_url

ALLOWED_DB_DRIVERS: frozenset[str] = frozenset({"postgresql+asyncpg", "sqlite+aiosqlite"})
JWT_SECRET_MIN_LENGTH = 32  # HS256: ключ не короче 256 бит


class Settings(BaseSettings):
    """Конфигурация приложения. Имена переменных окружения - имена полей в верхнем регистре."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,  # значения секретов не попадают в текст ошибок валидации
    )

    database_url: SecretStr
    jwt_secret: SecretStr
    jwt_ttl_minutes: int = Field(default=60, ge=1, le=1440)
    clinic_timezone: str = "Europe/Moscow"

    @field_validator("database_url")
    @classmethod
    def _check_database_url(cls, value: SecretStr) -> SecretStr:
        """URL должен быть корректным и указывать на асинхронный драйвер."""
        try:
            driver = make_url(value.get_secret_value()).drivername
        except ArgumentError as exc:
            raise ValueError("DATABASE_URL is not a valid SQLAlchemy URL") from exc
        if driver not in ALLOWED_DB_DRIVERS:
            raise ValueError(
                "DATABASE_URL must use an async driver: postgresql+asyncpg or sqlite+aiosqlite"
            )
        return value

    @field_validator("jwt_secret")
    @classmethod
    def _check_jwt_secret(cls, value: SecretStr) -> SecretStr:
        """Отсекает заведомо слабые секреты."""
        if len(value.get_secret_value()) < JWT_SECRET_MIN_LENGTH:
            raise ValueError(f"JWT_SECRET must be at least {JWT_SECRET_MIN_LENGTH} characters long")
        return value

    @field_validator("clinic_timezone")
    @classmethod
    def _check_timezone(cls, value: str) -> str:
        """Название часового пояса должно существовать в базе IANA."""
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
            raise ValueError(f"unknown IANA time zone: {value!r}") from exc
        return value

    @property
    def clinic_tz(self) -> ZoneInfo:
        """Часовой пояс клиники как объект ``ZoneInfo`` (для перевода UTC <-> местное время)."""
        return ZoneInfo(self.clinic_timezone)


@lru_cache
def get_settings() -> Settings:
    """Возвращает единственный (кешированный) экземпляр настроек.

    В тестах после смены переменных окружения вызовите ``get_settings.cache_clear()``.
    """
    return Settings()  # type: ignore[call-arg]
```

### app/db.py

```python
"""Асинхронный engine SQLAlchemy, фабрика сессий и зависимость ``get_session``.

Транзакциями управляет вызывающий код (сервисы / роуты): ``get_session`` сам ничего
не коммитит. Незавершённая транзакция откатывается при закрытии сессии.
"""

from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings


def create_engine(database_url: str) -> AsyncEngine:
    """Создаёт async engine.

    Для SQLite (тесты) включается ``PRAGMA foreign_keys=ON``: по умолчанию SQLite
    не проверяет внешние ключи, и тесты не ловили бы нарушения ссылочной целостности.
    """
    engine = create_async_engine(database_url, pool_pre_ping=True)

    if make_url(database_url).get_backend_name() == "sqlite":

        @event.listens_for(engine.sync_engine, "connect")
        def _enable_sqlite_foreign_keys(dbapi_connection: Any, _connection_record: Any) -> None:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
            finally:
                cursor.close()

    return engine


engine: AsyncEngine = create_engine(get_settings().database_url.get_secret_value())

SessionFactory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    engine,
    expire_on_commit=False,  # объекты остаются читаемыми после commit без повторного SELECT
)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI-зависимость: одна сессия на запрос, закрывается после ответа."""
    async with SessionFactory() as session:
        yield session


async def dispose_engine() -> None:
    """Закрывает пул соединений (вызывать при остановке приложения)."""
    await engine.dispose()
```

### app/models.py

```python
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
from datetime import date, datetime, time, timezone
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
    return datetime.now(timezone.utc)


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
        return value.astimezone(timezone.utc)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


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
```

### app/schemas.py

```python
"""Pydantic v2 схемы (Create / Update / Out) для всех сущностей.

* Create/Update - входные схемы: неизвестные поля запрещены (``extra="forbid"``),
  служебные поля (``created_by``, ``status`` при создании, ``created_at``,
  ``issued_at``) в них отсутствуют и задаются сервером.
* Update - семантика PATCH: отсутствующее поле = «не менять»; явный ``null`` допустим
  только для nullable-колонок (см. ``nullable_fields``). Применять через
  ``model_dump(exclude_unset=True)``.
* Out - выходные схемы (``from_attributes=True``). ``password_hash`` в них не
  объявлен и поэтому не может попасть в ответ.
* Все datetime - aware и приводятся к UTC.
"""

from datetime import date, datetime, time, timedelta, timezone
from typing import Annotated, ClassVar, Literal, Self

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StringConstraints,
    model_validator,
)

from app.models import AppointmentStatus, UserRole

# --------------------------------------------------------------------------- #
# Переиспользуемые типы с валидацией
# --------------------------------------------------------------------------- #

PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_BYTES = 72  # жёсткий предел bcrypt: всё, что длиннее, он не учитывает / отвергает
MAX_AGE_YEARS = 130


def _lower(value: str) -> str:
    return value.lower()


def _upper(value: str) -> str:
    return value.upper()


def _to_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


def _require_naive_time(value: time) -> time:
    if value.tzinfo is not None:
        raise ValueError("time must not contain a UTC offset: use clinic local time")
    return value


def _check_password(value: SecretStr) -> SecretStr:
    raw = value.get_secret_value()
    if len(raw) < PASSWORD_MIN_LENGTH:
        raise ValueError(f"password must be at least {PASSWORD_MIN_LENGTH} characters long")
    if len(raw.encode("utf-8")) > PASSWORD_MAX_BYTES:
        raise ValueError(f"password must not exceed {PASSWORD_MAX_BYTES} bytes in UTF-8")
    return value


def _check_login_password(value: SecretStr) -> SecretStr:
    # При входе правила сложности не раскрываем: только защита от пустого и гигантского ввода.
    if not 1 <= len(value.get_secret_value()) <= 1024:
        raise ValueError("invalid password length")
    return value


def _check_birth_date(value: date) -> date:
    # +1 день: допуск на часовые пояса клиники, опережающие UTC.
    today = datetime.now(timezone.utc).date()
    if value > today + timedelta(days=1):
        raise ValueError("birth_date must not be in the future")
    if value.year < today.year - MAX_AGE_YEARS:
        raise ValueError("birth_date is implausibly far in the past")
    return value


def _normalize_work_days(value: list[int]) -> list[int]:
    if len(set(value)) != len(value):
        raise ValueError("work_days must not contain duplicates")
    return sorted(value)


EntityId = Annotated[int, Field(gt=0, le=2_147_483_647)]  # границы INTEGER в PostgreSQL

# Email: pydantic[email] (email-validator) вне разрешённого стека, поэтому - строгий regex
# (RFC 5322 «dot-atom» + доменное имя с TLD из букв), нормализация в нижний регистр.
# Паттерн проверяется до нормализации, поэтому диапазоны заданы явно в обоих регистрах.
_EMAIL_PATTERN = (
    r"^[A-Za-z0-9_%+\-]+(?:\.[A-Za-z0-9_%+\-]+)*"
    r"@(?:[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$"
)
Email = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=6, max_length=254, pattern=_EMAIL_PATTERN),
    AfterValidator(_lower),
]
Password = Annotated[SecretStr, AfterValidator(_check_password)]
LoginPassword = Annotated[SecretStr, AfterValidator(_check_login_password)]

FullName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=255)]
Specialty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=100)]
Cabinet = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20)]
Medication = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
Dosage = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Reason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
Instructions = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
Notes = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=10_000)]

# Полис ОМС (единый номер): ровно 16 цифр. [0-9], а не \d - чтобы не пропускать юникодные цифры.
PolicyNumber = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^[0-9]{16}$")]

# E.164: "+", код страны не начинается с 0, всего 7-15 цифр.
PhoneE164 = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^\+[1-9][0-9]{6,14}$")]

# МКБ-10: латинская буква + 2 цифры + необязательная часть ".d" / ".dd" (J06.9, I10, S72.00).
# Подкод необязателен: ряд рубрик (например I10) его не имеет. Символы † и * не поддерживаются.
Icd10Code = Annotated[
    str,
    StringConstraints(strip_whitespace=True, pattern=r"^[A-Za-z][0-9]{2}(?:\.[0-9]{1,2})?$"),
    AfterValidator(_upper),
]

UTCDatetime = Annotated[AwareDatetime, AfterValidator(_to_utc)]
ClinicTime = Annotated[time, AfterValidator(_require_naive_time)]  # местное время клиники, без смещения
BirthDate = Annotated[date, AfterValidator(_check_birth_date)]
SlotMinutes = Annotated[int, Field(ge=5, le=240)]
# ISO-номера дней недели: 1 = понедельник ... 7 = воскресенье; без дублей, по возрастанию.
WorkDays = Annotated[
    list[Annotated[int, Field(ge=1, le=7)]],
    Field(min_length=1, max_length=7),
    AfterValidator(_normalize_work_days),
]


# --------------------------------------------------------------------------- #
# Базовые классы
# --------------------------------------------------------------------------- #


class _InputSchema(BaseModel):
    """Тело запроса: лишние поля запрещены (защита от mass assignment)."""

    model_config = ConfigDict(extra="forbid")


class _UpdateSchema(_InputSchema):
    """PATCH-схема: явный ``null`` разрешён только для полей из ``nullable_fields``."""

    nullable_fields: ClassVar[frozenset[str]] = frozenset()

    @model_validator(mode="after")
    def _reject_explicit_null(self) -> Self:
        for name in self.model_fields_set:
            if getattr(self, name) is None and name not in self.nullable_fields:
                raise ValueError(f"field '{name}' must not be null")
        return self


class _OutputSchema(BaseModel):
    """Ответ: создаётся из ORM-объекта через ``Model.model_validate(obj)``."""

    model_config = ConfigDict(from_attributes=True)


# --------------------------------------------------------------------------- #
# User
# --------------------------------------------------------------------------- #


class UserCreate(_InputSchema):
    """Создание пользователя (роль назначается администратором)."""

    email: Email
    password: Password
    full_name: FullName
    role: UserRole


class UserUpdate(_UpdateSchema):
    """Изменение пользователя. Ограничения «кто что может менять» - на уровне роутов."""

    email: Email | None = None
    password: Password | None = None
    full_name: FullName | None = None
    role: UserRole | None = None
    is_active: bool | None = None


class UserOut(_OutputSchema):
    """Пользователь в ответе API. Поля ``password_hash`` здесь намеренно нет."""

    id: int
    email: str
    full_name: str
    role: UserRole
    is_active: bool
    created_at: UTCDatetime


# --------------------------------------------------------------------------- #
# Doctor
# --------------------------------------------------------------------------- #


class DoctorCreate(_InputSchema):
    """Создание профиля врача. ``work_start`` / ``work_end`` - местное время клиники."""

    user_id: EntityId
    specialty: Specialty
    cabinet: Cabinet
    slot_minutes: SlotMinutes = 30
    work_start: ClinicTime
    work_end: ClinicTime
    work_days: WorkDays = Field(default_factory=lambda: [1, 2, 3, 4, 5])

    @model_validator(mode="after")
    def _check_shift(self) -> Self:
        if self.work_start >= self.work_end:
            raise ValueError("work_start must be earlier than work_end (overnight shifts are not supported)")
        return self


class DoctorUpdate(_UpdateSchema):
    """Изменение профиля врача (``user_id`` неизменяем)."""

    specialty: Specialty | None = None
    cabinet: Cabinet | None = None
    slot_minutes: SlotMinutes | None = None
    work_start: ClinicTime | None = None
    work_end: ClinicTime | None = None
    work_days: WorkDays | None = None

    @model_validator(mode="after")
    def _check_shift(self) -> Self:
        # Если передано только одно из значений, сравнение с сохранённым - в сервисном слое.
        if self.work_start is not None and self.work_end is not None and self.work_start >= self.work_end:
            raise ValueError("work_start must be earlier than work_end (overnight shifts are not supported)")
        return self


class DoctorOut(_OutputSchema):
    id: int
    user_id: int
    specialty: str
    cabinet: str
    slot_minutes: int
    work_start: time
    work_end: time
    work_days: list[int]


# --------------------------------------------------------------------------- #
# Patient
# --------------------------------------------------------------------------- #


class PatientCreate(_InputSchema):
    """Создание пациента (обычно регистратором; ``user_id`` - если у пациента есть аккаунт)."""

    user_id: EntityId | None = None
    full_name: FullName
    birth_date: BirthDate
    phone: PhoneE164 | None = None
    policy_number: PolicyNumber


class PatientUpdate(_UpdateSchema):
    nullable_fields: ClassVar[frozenset[str]] = frozenset({"user_id", "phone"})

    user_id: EntityId | None = None
    full_name: FullName | None = None
    birth_date: BirthDate | None = None
    phone: PhoneE164 | None = None
    policy_number: PolicyNumber | None = None


class PatientOut(_OutputSchema):
    """Содержит персональные данные: ограничивать доступ по ролям нужно в роутах."""

    id: int
    user_id: int | None
    full_name: str
    birth_date: date
    phone: str | None
    policy_number: str


# --------------------------------------------------------------------------- #
# Appointment
# --------------------------------------------------------------------------- #


class AppointmentCreate(_InputSchema):
    """Создание записи. ``status`` = scheduled и ``created_by`` (из токена) задаёт сервер."""

    doctor_id: EntityId
    patient_id: EntityId
    starts_at: UTCDatetime
    ends_at: UTCDatetime
    reason: Reason | None = None

    @model_validator(mode="after")
    def _check_interval(self) -> Self:
        if self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be later than starts_at")
        return self


class AppointmentUpdate(_UpdateSchema):
    """Перенос / смена статуса / причина. Врача и пациента менять нельзя - создаётся новая запись."""

    nullable_fields: ClassVar[frozenset[str]] = frozenset({"reason"})

    starts_at: UTCDatetime | None = None
    ends_at: UTCDatetime | None = None
    status: AppointmentStatus | None = None
    reason: Reason | None = None

    @model_validator(mode="after")
    def _check_interval(self) -> Self:
        # Если передана только одна граница, сравнение с сохранённой - в сервисном слое.
        if self.starts_at is not None and self.ends_at is not None and self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be later than starts_at")
        return self


class AppointmentOut(_OutputSchema):
    id: int
    doctor_id: int
    patient_id: int
    starts_at: UTCDatetime
    ends_at: UTCDatetime
    status: AppointmentStatus
    reason: str | None
    created_by: int
    created_at: UTCDatetime


# --------------------------------------------------------------------------- #
# Prescription
# --------------------------------------------------------------------------- #


class PrescriptionCreate(_InputSchema):
    """Создание назначения. ``issued_at`` проставляется сервером."""

    appointment_id: EntityId
    medication: Medication
    dosage: Dosage
    instructions: Instructions | None = None


class PrescriptionUpdate(_UpdateSchema):
    nullable_fields: ClassVar[frozenset[str]] = frozenset({"instructions"})

    medication: Medication | None = None
    dosage: Dosage | None = None
    instructions: Instructions | None = None


class PrescriptionOut(_OutputSchema):
    id: int
    appointment_id: int
    medication: str
    dosage: str
    instructions: str | None
    issued_at: UTCDatetime


# --------------------------------------------------------------------------- #
# MedicalRecord
# --------------------------------------------------------------------------- #


class MedicalRecordCreate(_InputSchema):
    """Создание записи в медкарте. ``created_at`` проставляется сервером."""

    patient_id: EntityId
    doctor_id: EntityId
    appointment_id: EntityId | None = None
    diagnosis_code: Icd10Code
    notes: Notes | None = None


class MedicalRecordUpdate(_UpdateSchema):
    nullable_fields: ClassVar[frozenset[str]] = frozenset({"notes"})

    diagnosis_code: Icd10Code | None = None
    notes: Notes | None = None


class MedicalRecordOut(_OutputSchema):
    """Содержит медицинские данные: ограничивать доступ по ролям нужно в роутах."""

    id: int
    patient_id: int
    doctor_id: int
    appointment_id: int | None
    diagnosis_code: str
    notes: str | None
    created_at: UTCDatetime


# --------------------------------------------------------------------------- #
# Аутентификация (вспомогательные схемы для логина)
# --------------------------------------------------------------------------- #


class LoginIn(_InputSchema):
    email: Email
    password: LoginPassword


class TokenOut(_OutputSchema):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int  # секунд
```

### app/security.py

```python
"""Безопасность: пароли (bcrypt), JWT (PyJWT, HS256) и FastAPI-зависимости авторизации.

* Пароли хэшируются bcrypt напрямую (без passlib). bcrypt учитывает только первые
  72 байта пароля, поэтому более длинные пароли отвергаются, а не обрезаются молча.
* Токен содержит поля ``sub`` (id пользователя строкой - PyJWT требует строку),
  ``role`` и ``exp``. Алгоритм при проверке жёстко ограничен HS256.
* Источник истины о правах - БД: роль из токена сверяется с текущей ролью пользователя,
  неактивные пользователи и токены с устаревшей ролью получают 401.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Annotated

import bcrypt
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.models import User, UserRole, utc_now
from app.schemas import PASSWORD_MAX_BYTES

JWT_ALGORITHM = "HS256"
BCRYPT_ROUNDS = 12

bearer_scheme = HTTPBearer(auto_error=False)  # ошибки формируем сами: всегда 401 + WWW-Authenticate


class TokenError(Exception):
    """Токен отсутствует в нужной форме, повреждён, просрочен или содержит неверные поля."""


@dataclass(frozen=True, slots=True)
class TokenPayload:
    """Проверенное содержимое access-токена."""

    user_id: int
    role: UserRole
    expires_at: datetime


# --------------------------------------------------------------------------- #
# Пароли
# --------------------------------------------------------------------------- #


def hash_password(password: str) -> str:
    """Возвращает bcrypt-хэш пароля (строка ``$2b$...``, соль внутри).

    Raises:
        ValueError: пароль длиннее 72 байт в UTF-8 (ограничение bcrypt).
    """
    raw = password.encode("utf-8")
    if len(raw) > PASSWORD_MAX_BYTES:
        raise ValueError(f"password must not exceed {PASSWORD_MAX_BYTES} bytes in UTF-8")
    return bcrypt.hashpw(raw, bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    """Проверяет пароль по хэшу. Любой некорректный ввод (в т.ч. битый хэш) даёт ``False``."""
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("ascii"))
    except ValueError:
        return False


async def hash_password_async(password: str) -> str:
    """``hash_password`` в пуле потоков: bcrypt нагружает CPU ~0.2-0.3 с и блокировал бы event loop."""
    return await asyncio.to_thread(hash_password, password)


async def verify_password_async(password: str, password_hash: str) -> bool:
    """``verify_password`` в пуле потоков (см. ``hash_password_async``)."""
    return await asyncio.to_thread(verify_password, password, password_hash)


# --------------------------------------------------------------------------- #
# JWT
# --------------------------------------------------------------------------- #


def create_access_token(user_id: int, role: UserRole, *, expires_delta: timedelta | None = None) -> str:
    """Создаёт подписанный JWT (HS256) с полями ``sub``, ``role``, ``exp``.

    ``expires_delta`` по умолчанию берётся из JWT_TTL_MINUTES; параметр нужен в основном тестам.
    """
    settings = get_settings()
    ttl = expires_delta if expires_delta is not None else timedelta(minutes=settings.jwt_ttl_minutes)
    claims = {
        "sub": str(user_id),
        "role": role.value,
        "exp": utc_now() + ttl,  # PyJWT сам превращает aware datetime в числовую метку времени
    }
    return jwt.encode(claims, settings.jwt_secret.get_secret_value(), algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> TokenPayload:
    """Проверяет подпись и срок действия токена и возвращает его содержимое.

    Допускается только HS256 (защита от подмены алгоритма, в т.ч. ``alg=none``);
    поля ``sub``, ``role``, ``exp`` обязательны.

    Raises:
        TokenError: токен недействителен по любой причине.
    """
    settings = get_settings()
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=[JWT_ALGORITHM],
            options={"require": ["sub", "role", "exp"]},
        )
        return TokenPayload(
            user_id=int(claims["sub"]),
            role=UserRole(claims["role"]),
            expires_at=datetime.fromtimestamp(claims["exp"], tz=timezone.utc),
        )
    except (jwt.PyJWTError, KeyError, ValueError, TypeError) as exc:
        raise TokenError("invalid token") from exc


# --------------------------------------------------------------------------- #
# Зависимости FastAPI
# --------------------------------------------------------------------------- #


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> User:
    """Возвращает пользователя по заголовку ``Authorization: Bearer <JWT>``.

    401, если токен отсутствует/недействителен, пользователь не найден или деактивирован,
    либо роль в токене не совпадает с текущей ролью в БД (роль изменили после выдачи токена).
    Сессия БД общая с остальными зависимостями того же запроса.
    """
    if credentials is None:
        raise _unauthorized("Not authenticated")
    try:
        payload = decode_access_token(credentials.credentials)
    except TokenError:
        raise _unauthorized("Invalid or expired token") from None

    user = await session.get(User, payload.user_id)
    if user is None or not user.is_active:
        raise _unauthorized("Invalid or expired token")
    if user.role != payload.role:
        raise _unauthorized("Token is no longer valid")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_roles(*roles: UserRole) -> Callable[..., Awaitable[User]]:
    """Фабрика зависимости: пропускает только пользователей с одной из перечисленных ролей.

    Пример: ``Depends(require_roles(UserRole.ADMIN, UserRole.REGISTRAR))``.
    Совпадение точное: администратор не получает доступ автоматически - его роль нужно
    указать явно. Неавторизованный получает 401, авторизованный с чужой ролью - 403.
    """
    if not roles:
        raise ValueError("require_roles() needs at least one role")
    allowed = frozenset(roles)

    async def dependency(user: CurrentUser) -> User:
        if user.role not in allowed:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")
        return user

    return dependency
```

### Допущения

1. **Конфигурация.** `DATABASE_URL` тоже без значения по умолчанию (дефолтный URL с паролем был бы секретом в коде) и допускает только async-драйверы `postgresql+asyncpg` / `sqlite+aiosqlite`; оба значения хранятся как `SecretStr`. `JWT_SECRET` не короче 32 символов. `JWT_TTL_MINUTES` по умолчанию 60 (допустимо 1-1440). На Windows для `zoneinfo` нужен пакет `tzdata`.
2. **Engine создаётся при импорте `app.db`.** Без `DATABASE_URL` / `JWT_SECRET` приложение не стартует. В тестах переменные окружения нужно задать до импорта (затем `get_settings.cache_clear()`) либо подменять `get_session` через `dependency_overrides`. `get_session` не делает commit - это обязанность сервисов и роутов.
3. **Время.** Тип `UTCDateTime` отвергает naive-значения и возвращает aware-UTC в том числе из SQLite. `work_start` / `work_end` - местное время клиники (CLINIC_TIMEZONE) без смещения; ночные смены не поддерживаются (`work_start < work_end`). Перевод в UTC выполняет слой расписания.
4. **`work_days`** - JSON-список ISO-номеров дней (1 = понедельник ... 7 = воскресенье), без дублей; присваивать список целиком.
5. **Что не покрывает частичный уникальный индекс.** Он запрещает только совпадающий `starts_at` у одного врача. Пересечение интервалов с разным началом, попадание в рабочие часы и сетку слотов, допустимые переходы статусов, согласованность `MedicalRecord.appointment_id` с врачом и пациентом - проверки сервисного слоя (в PostgreSQL надёжнее добавить в миграцию `EXCLUDE USING gist` по `tstzrange(starts_at, ends_at)` с `btree_gist`).
6. **Целостность и ORM.** Все FK - `ON DELETE RESTRICT`, физического удаления нет (деактивация через `is_active` / статус). Связи с `lazy="raise"`: загружать явно через `selectinload` / `joinedload`. Для Alembic - `Base.metadata` с naming convention. Проверка "16 цифр" и формата МКБ-10 в БД сведена к портативному CHECK по длине, сам формат проверяет Pydantic.
7. **Валидация.** Email - строгий regex с приведением к нижнему регистру, без `email-validator` (он вне заявленного стека). Телефон: `+`, затем 7-15 цифр, первая не 0; поле `phone` у пациента необязательное. Код МКБ-10: `[A-Z]\d{2}` + необязательное `.d` / `.dd` (подкод необязателен, т.к. есть рубрики вроде `I10`), приводится к верхнему регистру; символы † и * не поддерживаются. Дата рождения: не в будущем (допуск +1 день на часовые пояса) и не старше 130 лет. Пароль: 8+ символов и не более 72 байт UTF-8 (предел bcrypt).
8. **Create-схемы** не содержат `created_by`, `status`, `created_at`, `issued_at` - их задаёт сервер. `role` в `UserCreate` предполагает эндпоинт только для администратора. У `AppointmentCreate` оба поля `starts_at` / `ends_at` обязательны. `*Update` - PATCH-семантика: `null` допустим только для nullable-полей.
9. **JWT.** `sub` - id строкой (PyJWT требует строку); только поля `sub`, `role`, `exp`. Роль из токена сверяется с БД: смена роли или деактивация пользователя делает токен недействительным (401). `require_roles` требует точного совпадения роли, admin не обходит проверку. Схема `HTTPBearer`, а не `OAuth2PasswordBearer` (логин по JSON, без `python-multipart`).
10. **bcrypt.** Cost factor 12; для async-кода есть обёртки `hash_password_async` / `verify_password_async` (через `asyncio.to_thread`). Защита от тайминг-атаки при отсутствии пользователя (проверка по фиктивному хэшу) остаётся за роутом логина.
11. **Персональные и медицинские данные.** `PatientOut` и `MedicalRecordOut` отдают ПДн полностью - фильтрация по ролям и аудит доступа делаются в роутах. Шифрование полей в БД не реализовано (предполагается шифрование на уровне диска / СУБД).
12. **Сверх списка.** Добавлены `LoginIn` и `TokenOut` (понадобятся роуту логина), `dispose_engine()` для остановки приложения и `CurrentUser` как короткий алиас зависимости.

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

from datetime import UTC, date, datetime, time, timedelta
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
    # Значения у границ datetime (например, 0001-01-01T00:00+05:00) не переводятся в UTC:
    # OverflowError не является ValueError, Pydantic его не перехватывает, и клиент
    # получил бы 500 вместо 422.
    try:
        return value.astimezone(UTC)
    except OverflowError:
        raise ValueError("datetime is out of the supported range") from None


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
    today = datetime.now(UTC).date()
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
    token_type: Literal["bearer"] = "bearer"  # noqa: S105 - тип токена по RFC 6750, не секрет
    expires_in: int  # секунд

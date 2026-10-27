# Веб-приложение для клиники на FastAPI

Ниже готовое приложение «Клиника «Здоровье»»: REST API на FastAPI + SQLAlchemy 2.0 (SQLite), валидация на Pydantic v2, простой веб-интерфейс на чистом HTML/JS, который отдаётся самим FastAPI, и набор тестов на pytest.

## Что умеет приложение

- **Врачи**: список с фильтром по специальности и поиском по ФИО, создание, просмотр, изменение, удаление.
- **Пациенты**: то же самое плюс история приёмов пациента. Телефон уникален.
- **Приёмы (записи)**: создание, перенос, смена статуса (`scheduled` -> `completed` / `cancelled`), заключение врача, удаление.
- **Свободные слоты** врача на выбранный день (`GET /api/doctors/{id}/slots`).
- **Бизнес-правила**:
  - клиника работает с 08:00 до 20:00, приём должен целиком помещаться в этот интервал и в один день;
  - нельзя записаться на прошедшее время;
  - у врача не может быть двух пересекающихся запланированных приёмов (приёмы «встык» разрешены);
  - у пациента тоже не может быть двух пересекающихся приёмов;
  - врача и пациента с приёмами удалить нельзя (409);
  - статус завершённого или отменённого приёма изменить нельзя, завершённый приём удалить нельзя (это медицинская запись).
- **Веб-интерфейс** на `/` (запись на приём, расписание, справочники) и автоматическая документация Swagger на `/docs`.
- При первом запуске база заполняется демонстрационными врачами и пациентами.

Требуется Python 3.10 или новее.

## Структура проекта

```
clinic/
├── pytest.ini
├── requirements.txt
├── requirements-dev.txt
├── app/
│   ├── __init__.py
│   ├── main.py            # создание приложения, обработчик ошибок, статика
│   ├── database.py        # движок SQLAlchemy, сессия, настройки SQLite
│   ├── models.py          # ORM-модели
│   ├── schemas.py         # Pydantic-схемы
│   ├── errors.py          # доменные исключения
│   ├── scheduling.py      # часы работы, конфликты, свободные слоты
│   ├── crud.py            # операции с БД и бизнес-логика
│   ├── seed.py            # демонстрационные данные
│   ├── routers/
│   │   ├── __init__.py
│   │   ├── doctors.py
│   │   ├── patients.py
│   │   └── appointments.py
│   └── static/
│       ├── index.html
│       ├── style.css
│       └── app.js
└── tests/
    └── test_api.py
```

Файлы `app/__init__.py` и `app/routers/__init__.py` пустые.

## Зависимости

`requirements.txt`

```text
fastapi>=0.115
uvicorn[standard]>=0.30
sqlalchemy>=2.0
pydantic>=2.7
```

`requirements-dev.txt`

```text
-r requirements.txt
pytest>=8
httpx>=0.27
```

`pytest.ini`

```ini
[pytest]
pythonpath = .
testpaths = tests
```

## Код приложения

### `app/errors.py`

Доменные исключения. Бизнес-логика бросает их, а единый обработчик в `main.py` превращает их в JSON-ответы с нужным HTTP-кодом.

```python
class ClinicError(Exception):
    """Базовая ошибка бизнес-логики; превращается в JSON-ответ обработчиком в main.py."""

    status_code = 400

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class NotFoundError(ClinicError):
    status_code = 404


class ConflictError(ClinicError):
    status_code = 409


class BusinessRuleError(ClinicError):
    status_code = 422
```

### `app/database.py`

SQLite не умеет регистронезависимый поиск по кириллице (`LIKE` и `lower()` работают только для ASCII). Поэтому при каждом подключении регистрируется Python-функция `py_lower`, которую используют поисковые запросы. Заодно включаются внешние ключи.

```python
import sqlite3

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DATABASE_URL = "sqlite:///./clinic.db"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False)


class Base(DeclarativeBase):
    pass


@event.listens_for(Engine, "connect")
def _configure_sqlite(dbapi_connection, _record) -> None:
    if not isinstance(dbapi_connection, sqlite3.Connection):
        return
    # Регистронезависимый поиск по кириллице: встроенный lower() в SQLite её не поддерживает.
    dbapi_connection.create_function(
        "py_lower",
        1,
        lambda value: value.lower() if isinstance(value, str) else value,
        deterministic=True,
    )
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def get_db():
    """Зависимость FastAPI: одна сессия на запрос."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
```

### `app/models.py`

В приёме хранится и `end_time` (вычисляется при создании), чтобы проверка пересечений делалась простым SQL-условием `start < new_end AND end > new_start`.

```python
import enum
from datetime import date, datetime

from sqlalchemy import Date, DateTime, Enum, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


class AppointmentStatus(str, enum.Enum):
    scheduled = "scheduled"  # запланирован
    completed = "completed"  # состоялся
    cancelled = "cancelled"  # отменён


class Doctor(Base):
    __tablename__ = "doctors"

    id: Mapped[int] = mapped_column(primary_key=True)
    full_name: Mapped[str] = mapped_column(String(120))
    specialty: Mapped[str] = mapped_column(String(80), index=True)
    cabinet: Mapped[str | None] = mapped_column(String(20), default=None)

    appointments: Mapped[list["Appointment"]] = relationship(back_populates="doctor")


class Patient(Base):
    __tablename__ = "patients"

    id: Mapped[int] = mapped_column(primary_key=True)
    full_name: Mapped[str] = mapped_column(String(120))
    birth_date: Mapped[date] = mapped_column(Date)
    phone: Mapped[str] = mapped_column(String(20), unique=True)
    email: Mapped[str | None] = mapped_column(String(120), default=None)

    appointments: Mapped[list["Appointment"]] = relationship(back_populates="patient")


class Appointment(Base):
    __tablename__ = "appointments"

    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    doctor_id: Mapped[int] = mapped_column(ForeignKey("doctors.id"), index=True)
    start_time: Mapped[datetime] = mapped_column(DateTime, index=True)
    end_time: Mapped[datetime] = mapped_column(DateTime)
    duration_minutes: Mapped[int] = mapped_column(default=30)
    reason: Mapped[str] = mapped_column(String(300))
    status: Mapped[AppointmentStatus] = mapped_column(
        Enum(AppointmentStatus), default=AppointmentStatus.scheduled, index=True
    )
    conclusion: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    patient: Mapped[Patient] = relationship(back_populates="appointments")
    doctor: Mapped[Doctor] = relationship(back_populates="appointments")
```

### `app/schemas.py`

Общие ограничения полей вынесены в `Annotated`-алиасы и используются и в схемах создания, и в схемах частичного обновления (`PATCH`).

```python
from datetime import date, datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .models import AppointmentStatus

PHONE_PATTERN = r"^\+?\d{10,15}$"
EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"

FullName = Annotated[str, Field(min_length=3, max_length=120, examples=["Иванов Иван Иванович"])]
Specialty = Annotated[str, Field(min_length=2, max_length=80, examples=["Терапевт"])]
Cabinet = Annotated[str, Field(max_length=20, examples=["101"])]
Phone = Annotated[str, Field(pattern=PHONE_PATTERN, examples=["+79991234567"])]
Email = Annotated[str, Field(max_length=120, pattern=EMAIL_PATTERN)]
Reason = Annotated[str, Field(min_length=3, max_length=300, examples=["Боль в горле"])]
Conclusion = Annotated[str, Field(max_length=2000)]
Duration = Annotated[int, Field(ge=10, le=120, description="Длительность приёма, минут")]


class ApiModel(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)


# ---------- Врачи ----------

class DoctorBase(ApiModel):
    full_name: FullName
    specialty: Specialty
    cabinet: Cabinet | None = None


class DoctorCreate(DoctorBase):
    pass


class DoctorUpdate(ApiModel):
    full_name: FullName | None = None
    specialty: Specialty | None = None
    cabinet: Cabinet | None = None


class DoctorOut(DoctorBase):
    model_config = ConfigDict(from_attributes=True)

    id: int


class DoctorShort(ApiModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    full_name: str
    specialty: str


# ---------- Пациенты ----------

def _check_birth_date(value: date | None) -> date | None:
    if value is None:
        return value
    if value > date.today():
        raise ValueError("Дата рождения не может быть в будущем")
    if value.year < 1900:
        raise ValueError("Дата рождения слишком ранняя")
    return value


class PatientBase(ApiModel):
    full_name: FullName
    birth_date: date
    phone: Phone
    email: Email | None = None

    @field_validator("birth_date")
    @classmethod
    def _birth_date(cls, value: date) -> date:
        return _check_birth_date(value)


class PatientCreate(PatientBase):
    pass


class PatientUpdate(ApiModel):
    full_name: FullName | None = None
    birth_date: date | None = None
    phone: Phone | None = None
    email: Email | None = None

    @field_validator("birth_date")
    @classmethod
    def _birth_date(cls, value: date | None) -> date | None:
        return _check_birth_date(value)


class PatientOut(PatientBase):
    model_config = ConfigDict(from_attributes=True)

    id: int


class PatientShort(ApiModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    full_name: str


# ---------- Приёмы ----------

def _normalize_start(value: datetime | None) -> datetime | None:
    if value is None:
        return value
    if value.tzinfo is not None:
        raise ValueError("Укажите местное время клиники без часового пояса")
    return value.replace(second=0, microsecond=0)


class AppointmentCreate(ApiModel):
    patient_id: int = Field(gt=0)
    doctor_id: int = Field(gt=0)
    start_time: datetime
    duration_minutes: Duration = 30
    reason: Reason

    @field_validator("start_time")
    @classmethod
    def _start_time(cls, value: datetime) -> datetime:
        return _normalize_start(value)


class AppointmentUpdate(ApiModel):
    start_time: datetime | None = None
    duration_minutes: Duration | None = None
    reason: Reason | None = None
    status: AppointmentStatus | None = None
    conclusion: Conclusion | None = None

    @field_validator("start_time")
    @classmethod
    def _start_time(cls, value: datetime | None) -> datetime | None:
        return _normalize_start(value)


class AppointmentOut(ApiModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    start_time: datetime
    end_time: datetime
    duration_minutes: int
    reason: str
    status: AppointmentStatus
    conclusion: str | None
    patient: PatientShort
    doctor: DoctorShort


class SlotsOut(ApiModel):
    doctor_id: int
    day: date
    duration_minutes: int
    slots: list[datetime]
```

### `app/scheduling.py`

Правила расписания в одном месте: часы работы, поиск пересечений, генерация свободных слотов.

```python
from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import InstrumentedAttribute, Session

from .errors import BusinessRuleError, ConflictError
from .models import Appointment, AppointmentStatus

WORK_START = time(8, 0)
WORK_END = time(20, 0)
SLOT_STEP = timedelta(minutes=30)


def ensure_in_working_hours(start: datetime, end: datetime) -> None:
    if start.date() != end.date() or start.time() < WORK_START or end.time() > WORK_END:
        raise BusinessRuleError(
            f"Приём должен целиком укладываться в часы работы клиники: "
            f"{WORK_START:%H:%M}-{WORK_END:%H:%M}"
        )


def _overlap_exists(
    db: Session,
    owner_column: InstrumentedAttribute,
    owner_id: int,
    start: datetime,
    end: datetime,
    exclude_id: int | None,
) -> bool:
    """Есть ли у врача (или пациента) запланированный приём, пересекающийся с [start, end)."""
    stmt = select(Appointment.id).where(
        owner_column == owner_id,
        Appointment.status == AppointmentStatus.scheduled,
        Appointment.start_time < end,
        Appointment.end_time > start,
    )
    if exclude_id is not None:
        stmt = stmt.where(Appointment.id != exclude_id)
    return db.scalar(stmt.limit(1)) is not None


def ensure_slot_available(
    db: Session,
    *,
    doctor_id: int,
    patient_id: int,
    start: datetime,
    end: datetime,
    exclude_id: int | None = None,
) -> None:
    if start <= datetime.now():
        raise BusinessRuleError("Нельзя записать на прошедшее время")
    ensure_in_working_hours(start, end)
    if _overlap_exists(db, Appointment.doctor_id, doctor_id, start, end, exclude_id):
        raise ConflictError("У врача уже есть приём в это время")
    if _overlap_exists(db, Appointment.patient_id, patient_id, start, end, exclude_id):
        raise ConflictError("У пациента уже есть приём в это время")


def free_slots(db: Session, doctor_id: int, day: date, duration_minutes: int) -> list[datetime]:
    """Свободные начала приёма с шагом 30 минут на указанный день."""
    day_start = datetime.combine(day, WORK_START)
    day_end = datetime.combine(day, WORK_END)
    busy = db.execute(
        select(Appointment.start_time, Appointment.end_time).where(
            Appointment.doctor_id == doctor_id,
            Appointment.status == AppointmentStatus.scheduled,
            Appointment.start_time < day_end,
            Appointment.end_time > day_start,
        )
    ).all()

    now = datetime.now()
    duration = timedelta(minutes=duration_minutes)
    result: list[datetime] = []
    slot = day_start
    while slot + duration <= day_end:
        end = slot + duration
        if slot > now and not any(b_start < end and b_end > slot for b_start, b_end in busy):
            result.append(slot)
        slot += SLOT_STEP
    return result
```

### `app/crud.py`

```python
from datetime import date, datetime, time, timedelta
from typing import Any, TypeVar

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import InstrumentedAttribute, Session, joinedload

from . import schemas, scheduling
from .errors import ConflictError, NotFoundError
from .models import Appointment, AppointmentStatus, Doctor, Patient

ModelT = TypeVar("ModelT")


# ---------- Вспомогательное ----------

def get_or_404(db: Session, model: type[ModelT], obj_id: int, title: str) -> ModelT:
    obj = db.get(model, obj_id)
    if obj is None:
        raise NotFoundError(f"{title} с id={obj_id} не найден")
    return obj


def _contains(column: InstrumentedAttribute, text: str):
    """Регистронезависимый поиск подстроки (работает и с кириллицей, см. database.py)."""
    escaped = text.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return func.py_lower(column).like(f"%{escaped}%", escape="\\")


def _apply(obj: Any, data: dict[str, Any], nullable: frozenset[str] = frozenset()) -> None:
    """Применяет частичное обновление; null игнорируется для обязательных полей."""
    for key, value in data.items():
        if value is None and key not in nullable:
            continue
        setattr(obj, key, value)


def _commit(db: Session, conflict_message: str) -> None:
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ConflictError(conflict_message) from exc


def _has_appointments(db: Session, column: InstrumentedAttribute, owner_id: int) -> bool:
    return db.scalar(select(Appointment.id).where(column == owner_id).limit(1)) is not None


# ---------- Врачи ----------

def list_doctors(
    db: Session, *, specialty: str | None, q: str | None, skip: int, limit: int
) -> list[Doctor]:
    stmt = select(Doctor).order_by(Doctor.full_name)
    if specialty:
        stmt = stmt.where(func.py_lower(Doctor.specialty) == specialty.lower())
    if q:
        stmt = stmt.where(_contains(Doctor.full_name, q))
    return list(db.scalars(stmt.offset(skip).limit(limit)))


def create_doctor(db: Session, data: schemas.DoctorCreate) -> Doctor:
    doctor = Doctor(**data.model_dump())
    db.add(doctor)
    db.commit()
    db.refresh(doctor)
    return doctor


def update_doctor(db: Session, doctor_id: int, data: schemas.DoctorUpdate) -> Doctor:
    doctor = get_or_404(db, Doctor, doctor_id, "Врач")
    _apply(doctor, data.model_dump(exclude_unset=True), nullable=frozenset({"cabinet"}))
    db.commit()
    db.refresh(doctor)
    return doctor


def delete_doctor(db: Session, doctor_id: int) -> None:
    doctor = get_or_404(db, Doctor, doctor_id, "Врач")
    if _has_appointments(db, Appointment.doctor_id, doctor_id):
        raise ConflictError("Нельзя удалить врача, у которого есть приёмы")
    db.delete(doctor)
    db.commit()


def get_doctor_slots(db: Session, doctor_id: int, day: date, duration_minutes: int) -> dict:
    get_or_404(db, Doctor, doctor_id, "Врач")
    return {
        "doctor_id": doctor_id,
        "day": day,
        "duration_minutes": duration_minutes,
        "slots": scheduling.free_slots(db, doctor_id, day, duration_minutes),
    }


# ---------- Пациенты ----------

PHONE_TAKEN = "Пациент с таким телефоном уже зарегистрирован"


def _ensure_phone_free(db: Session, phone: str, exclude_id: int | None = None) -> None:
    stmt = select(Patient.id).where(Patient.phone == phone)
    if exclude_id is not None:
        stmt = stmt.where(Patient.id != exclude_id)
    if db.scalar(stmt.limit(1)) is not None:
        raise ConflictError(PHONE_TAKEN)


def list_patients(db: Session, *, q: str | None, skip: int, limit: int) -> list[Patient]:
    stmt = select(Patient).order_by(Patient.full_name)
    if q:
        stmt = stmt.where(_contains(Patient.full_name, q) | Patient.phone.contains(q))
    return list(db.scalars(stmt.offset(skip).limit(limit)))


def create_patient(db: Session, data: schemas.PatientCreate) -> Patient:
    _ensure_phone_free(db, data.phone)
    patient = Patient(**data.model_dump())
    db.add(patient)
    _commit(db, PHONE_TAKEN)
    db.refresh(patient)
    return patient


def update_patient(db: Session, patient_id: int, data: schemas.PatientUpdate) -> Patient:
    patient = get_or_404(db, Patient, patient_id, "Пациент")
    changes = data.model_dump(exclude_unset=True)
    if changes.get("phone"):
        _ensure_phone_free(db, changes["phone"], exclude_id=patient_id)
    _apply(patient, changes, nullable=frozenset({"email"}))
    _commit(db, PHONE_TAKEN)
    db.refresh(patient)
    return patient


def delete_patient(db: Session, patient_id: int) -> None:
    patient = get_or_404(db, Patient, patient_id, "Пациент")
    if _has_appointments(db, Appointment.patient_id, patient_id):
        raise ConflictError("Нельзя удалить пациента, у которого есть приёмы")
    db.delete(patient)
    db.commit()


# ---------- Приёмы ----------

def _appointment_select():
    return select(Appointment).options(
        joinedload(Appointment.patient), joinedload(Appointment.doctor)
    )


def get_appointment(db: Session, appointment_id: int) -> Appointment:
    appointment = db.scalar(_appointment_select().where(Appointment.id == appointment_id))
    if appointment is None:
        raise NotFoundError(f"Приём с id={appointment_id} не найден")
    return appointment


def list_appointments(
    db: Session,
    *,
    doctor_id: int | None = None,
    patient_id: int | None = None,
    day: date | None = None,
    status: AppointmentStatus | None = None,
    skip: int = 0,
    limit: int = 100,
) -> list[Appointment]:
    stmt = _appointment_select().order_by(Appointment.start_time)
    if doctor_id is not None:
        stmt = stmt.where(Appointment.doctor_id == doctor_id)
    if patient_id is not None:
        stmt = stmt.where(Appointment.patient_id == patient_id)
    if status is not None:
        stmt = stmt.where(Appointment.status == status)
    if day is not None:
        day_start = datetime.combine(day, time.min)
        stmt = stmt.where(
            Appointment.start_time >= day_start,
            Appointment.start_time < day_start + timedelta(days=1),
        )
    return list(db.scalars(stmt.offset(skip).limit(limit)))


def create_appointment(db: Session, data: schemas.AppointmentCreate) -> Appointment:
    get_or_404(db, Patient, data.patient_id, "Пациент")
    get_or_404(db, Doctor, data.doctor_id, "Врач")

    end = data.start_time + timedelta(minutes=data.duration_minutes)
    scheduling.ensure_slot_available(
        db,
        doctor_id=data.doctor_id,
        patient_id=data.patient_id,
        start=data.start_time,
        end=end,
    )

    appointment = Appointment(
        patient_id=data.patient_id,
        doctor_id=data.doctor_id,
        start_time=data.start_time,
        end_time=end,
        duration_minutes=data.duration_minutes,
        reason=data.reason,
    )
    db.add(appointment)
    db.commit()
    return get_appointment(db, appointment.id)


def update_appointment(
    db: Session, appointment_id: int, data: schemas.AppointmentUpdate
) -> Appointment:
    appointment = get_appointment(db, appointment_id)
    # null допустим только для заключения (его можно очистить)
    changes = {
        key: value
        for key, value in data.model_dump(exclude_unset=True).items()
        if value is not None or key == "conclusion"
    }

    new_status = changes.get("status")
    if appointment.status != AppointmentStatus.scheduled:
        if new_status is not None and new_status != appointment.status:
            raise ConflictError("Статус завершённого или отменённого приёма изменить нельзя")
        if "start_time" in changes or "duration_minutes" in changes:
            raise ConflictError("Перенести можно только запланированный приём")

    if "start_time" in changes or "duration_minutes" in changes:
        start = changes.get("start_time", appointment.start_time)
        duration = changes.get("duration_minutes", appointment.duration_minutes)
        end = start + timedelta(minutes=duration)
        scheduling.ensure_slot_available(
            db,
            doctor_id=appointment.doctor_id,
            patient_id=appointment.patient_id,
            start=start,
            end=end,
            exclude_id=appointment.id,
        )
        appointment.start_time = start
        appointment.duration_minutes = duration
        appointment.end_time = end

    for field in ("reason", "status", "conclusion"):
        if field in changes:
            setattr(appointment, field, changes[field])

    db.commit()
    return get_appointment(db, appointment.id)


def delete_appointment(db: Session, appointment_id: int) -> None:
    appointment = get_appointment(db, appointment_id)
    if appointment.status == AppointmentStatus.completed:
        raise ConflictError("Завершённые приёмы удалять нельзя: это медицинская запись")
    db.delete(appointment)
    db.commit()
```

### `app/routers/doctors.py`

```python
from datetime import date

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from .. import crud, schemas
from ..database import get_db
from ..models import Doctor

router = APIRouter(prefix="/api/doctors", tags=["Врачи"])


@router.get("", response_model=list[schemas.DoctorOut], summary="Список врачей")
def list_doctors(
    specialty: str | None = Query(None, description="Специальность (точное совпадение)"),
    q: str | None = Query(None, description="Поиск по части ФИО"),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    return crud.list_doctors(db, specialty=specialty, q=q, skip=skip, limit=limit)


@router.post(
    "",
    response_model=schemas.DoctorOut,
    status_code=status.HTTP_201_CREATED,
    summary="Добавить врача",
)
def create_doctor(data: schemas.DoctorCreate, db: Session = Depends(get_db)):
    return crud.create_doctor(db, data)


@router.get("/{doctor_id}", response_model=schemas.DoctorOut, summary="Карточка врача")
def get_doctor(doctor_id: int, db: Session = Depends(get_db)):
    return crud.get_or_404(db, Doctor, doctor_id, "Врач")


@router.patch("/{doctor_id}", response_model=schemas.DoctorOut, summary="Изменить врача")
def update_doctor(doctor_id: int, data: schemas.DoctorUpdate, db: Session = Depends(get_db)):
    return crud.update_doctor(db, doctor_id, data)


@router.delete(
    "/{doctor_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Удалить врача"
)
def delete_doctor(doctor_id: int, db: Session = Depends(get_db)) -> None:
    crud.delete_doctor(db, doctor_id)


@router.get(
    "/{doctor_id}/slots",
    response_model=schemas.SlotsOut,
    summary="Свободное время врача на день",
)
def doctor_slots(
    doctor_id: int,
    day: date = Query(..., description="День в формате YYYY-MM-DD"),
    duration: int = Query(30, ge=10, le=120, description="Длительность приёма, минут"),
    db: Session = Depends(get_db),
):
    return crud.get_doctor_slots(db, doctor_id, day, duration)
```

### `app/routers/patients.py`

```python
from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from .. import crud, schemas
from ..database import get_db
from ..models import Patient

router = APIRouter(prefix="/api/patients", tags=["Пациенты"])


@router.get("", response_model=list[schemas.PatientOut], summary="Список пациентов")
def list_patients(
    q: str | None = Query(None, description="Поиск по части ФИО или телефона"),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    return crud.list_patients(db, q=q, skip=skip, limit=limit)


@router.post(
    "",
    response_model=schemas.PatientOut,
    status_code=status.HTTP_201_CREATED,
    summary="Зарегистрировать пациента",
)
def create_patient(data: schemas.PatientCreate, db: Session = Depends(get_db)):
    return crud.create_patient(db, data)


@router.get("/{patient_id}", response_model=schemas.PatientOut, summary="Карточка пациента")
def get_patient(patient_id: int, db: Session = Depends(get_db)):
    return crud.get_or_404(db, Patient, patient_id, "Пациент")


@router.patch("/{patient_id}", response_model=schemas.PatientOut, summary="Изменить пациента")
def update_patient(patient_id: int, data: schemas.PatientUpdate, db: Session = Depends(get_db)):
    return crud.update_patient(db, patient_id, data)


@router.delete(
    "/{patient_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Удалить пациента"
)
def delete_patient(patient_id: int, db: Session = Depends(get_db)) -> None:
    crud.delete_patient(db, patient_id)


@router.get(
    "/{patient_id}/appointments",
    response_model=list[schemas.AppointmentOut],
    summary="История приёмов пациента",
)
def patient_appointments(patient_id: int, db: Session = Depends(get_db)):
    crud.get_or_404(db, Patient, patient_id, "Пациент")
    return crud.list_appointments(db, patient_id=patient_id, limit=500)
```

### `app/routers/appointments.py`

Параметр фильтра по статусу назван `status_filter` с псевдонимом `status`, чтобы не затенять `fastapi.status`.

```python
from datetime import date

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from .. import crud, schemas
from ..database import get_db
from ..models import AppointmentStatus

router = APIRouter(prefix="/api/appointments", tags=["Приёмы"])


@router.get("", response_model=list[schemas.AppointmentOut], summary="Список приёмов")
def list_appointments(
    doctor_id: int | None = None,
    patient_id: int | None = None,
    day: date | None = Query(None, description="Фильтр по дню, YYYY-MM-DD"),
    status_filter: AppointmentStatus | None = Query(None, alias="status"),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    return crud.list_appointments(
        db,
        doctor_id=doctor_id,
        patient_id=patient_id,
        day=day,
        status=status_filter,
        skip=skip,
        limit=limit,
    )


@router.post(
    "",
    response_model=schemas.AppointmentOut,
    status_code=status.HTTP_201_CREATED,
    summary="Записать пациента на приём",
)
def create_appointment(data: schemas.AppointmentCreate, db: Session = Depends(get_db)):
    return crud.create_appointment(db, data)


@router.get("/{appointment_id}", response_model=schemas.AppointmentOut, summary="Приём по id")
def get_appointment(appointment_id: int, db: Session = Depends(get_db)):
    return crud.get_appointment(db, appointment_id)


@router.patch(
    "/{appointment_id}",
    response_model=schemas.AppointmentOut,
    summary="Перенести, завершить или отменить приём",
)
def update_appointment(
    appointment_id: int, data: schemas.AppointmentUpdate, db: Session = Depends(get_db)
):
    return crud.update_appointment(db, appointment_id, data)


@router.delete(
    "/{appointment_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Удалить приём"
)
def delete_appointment(appointment_id: int, db: Session = Depends(get_db)) -> None:
    crud.delete_appointment(db, appointment_id)
```

### `app/seed.py`

```python
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Doctor, Patient


def seed_if_empty(db: Session) -> None:
    """Заполняет пустую базу демонстрационными данными."""
    if db.scalar(select(func.count(Doctor.id))):
        return
    db.add_all(
        [
            Doctor(full_name="Иванов Сергей Петрович", specialty="Терапевт", cabinet="101"),
            Doctor(full_name="Смирнова Ольга Викторовна", specialty="Кардиолог", cabinet="204"),
            Doctor(full_name="Кузнецов Андрей Игоревич", specialty="Невролог", cabinet="215"),
            Doctor(full_name="Орлова Мария Сергеевна", specialty="Педиатр", cabinet="110"),
            Patient(
                full_name="Петров Пётр Петрович",
                birth_date=date(1985, 3, 14),
                phone="+79990000001",
            ),
            Patient(
                full_name="Сидорова Анна Михайловна",
                birth_date=date(1992, 11, 2),
                phone="+79990000002",
                email="anna@example.com",
            ),
            Patient(
                full_name="Громов Дмитрий Алексеевич",
                birth_date=date(2015, 7, 21),
                phone="+79990000003",
            ),
        ]
    )
    db.commit()
```

### `app/main.py`

```python
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import models  # noqa: F401  (регистрирует модели в Base.metadata)
from .database import Base, SessionLocal, engine
from .errors import ClinicError
from .routers import appointments, doctors, patients
from .seed import seed_if_empty

BASE_DIR = Path(__file__).resolve().parent


@asynccontextmanager
async def lifespan(_app: FastAPI):
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        seed_if_empty(db)
    yield


app = FastAPI(
    title="Клиника «Здоровье»",
    description="Учёт врачей, пациентов и записей на приём.",
    version="1.0.0",
    lifespan=lifespan,
)


@app.exception_handler(ClinicError)
async def clinic_error_handler(_request: Request, exc: ClinicError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


app.include_router(doctors.router)
app.include_router(patients.router)
app.include_router(appointments.router)

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


@app.get("/api/health", tags=["Служебное"], summary="Проверка работоспособности")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(BASE_DIR / "static" / "index.html")
```

## Веб-интерфейс

Данные вставляются в страницу только через `textContent` и `append` (текстовые узлы), поэтому интерфейс не подвержен XSS.

### `app/static/index.html`

```html
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Клиника «Здоровье»</title>
  <link rel="stylesheet" href="/static/style.css">
</head>
<body>
  <header>
    <h1>Клиника «Здоровье»</h1>
    <nav>
      <button data-tab="appointments" class="active">Приёмы</button>
      <button data-tab="doctors">Врачи</button>
      <button data-tab="patients">Пациенты</button>
      <a href="/docs" target="_blank" rel="noopener">API-документация</a>
    </nav>
  </header>

  <div id="message" role="status" hidden></div>

  <main>
    <section id="tab-appointments">
      <h2>Запись на приём</h2>
      <form id="appt-form" class="grid">
        <label>Пациент <select id="appt-patient" required></select></label>
        <label>Врач <select id="appt-doctor" required></select></label>
        <label>Дата <input type="date" id="appt-day" required></label>
        <label>Длительность, мин
          <select id="appt-duration">
            <option>30</option>
            <option>45</option>
            <option>60</option>
          </select>
        </label>
        <label>Время <select id="appt-slot" required></select></label>
        <label>Причина обращения
          <input id="appt-reason" required minlength="3" maxlength="300">
        </label>
        <button type="submit">Записать</button>
      </form>

      <h2>Расписание</h2>
      <div class="toolbar">
        <label>День <input type="date" id="filter-day"></label>
        <button id="filter-reset" type="button">Все дни</button>
      </div>
      <div class="table-wrap"><table id="appt-table"></table></div>
    </section>

    <section id="tab-doctors" hidden>
      <h2>Новый врач</h2>
      <form id="doctor-form" class="grid">
        <label>ФИО <input name="full_name" required minlength="3" maxlength="120"></label>
        <label>Специальность <input name="specialty" required minlength="2" maxlength="80"></label>
        <label>Кабинет <input name="cabinet" maxlength="20"></label>
        <button type="submit">Добавить</button>
      </form>
      <h2>Врачи</h2>
      <div class="table-wrap"><table id="doctor-table"></table></div>
    </section>

    <section id="tab-patients" hidden>
      <h2>Новый пациент</h2>
      <form id="patient-form" class="grid">
        <label>ФИО <input name="full_name" required minlength="3" maxlength="120"></label>
        <label>Дата рождения <input type="date" name="birth_date" required></label>
        <label>Телефон
          <input name="phone" required pattern="\+?\d{10,15}" placeholder="+79991234567">
        </label>
        <label>Email <input type="email" name="email" maxlength="120"></label>
        <button type="submit">Добавить</button>
      </form>
      <h2>Пациенты</h2>
      <div class="table-wrap"><table id="patient-table"></table></div>
    </section>
  </main>

  <script src="/static/app.js"></script>
</body>
</html>
```

### `app/static/style.css`

```css
:root {
  --bg: #f4f7f9;
  --card: #ffffff;
  --text: #1d2b36;
  --accent: #1f7a8c;
  --danger: #c0392b;
  --border: #d5dde3;
}

* { box-sizing: border-box; }

body {
  margin: 0;
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  background: var(--bg);
  color: var(--text);
}

header {
  background: var(--accent);
  color: #fff;
  padding: 12px 24px;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 16px 32px;
}

header h1 { margin: 0; font-size: 1.4rem; }
nav { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
nav a { color: #fff; margin-left: 12px; }

nav button {
  background: transparent;
  color: #fff;
  border: 1px solid rgba(255, 255, 255, 0.6);
}
nav button.active { background: #fff; color: var(--accent); }

main { max-width: 1100px; margin: 24px auto; padding: 0 16px; }
section { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 16px 20px; }
h2 { margin: 16px 0 8px; font-size: 1.1rem; }
h2:first-child { margin-top: 0; }

.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 12px; align-items: end; }
.toolbar { display: flex; gap: 12px; align-items: end; margin-bottom: 8px; }

label { display: flex; flex-direction: column; gap: 4px; font-size: 0.9rem; }
input, select { padding: 7px 8px; border: 1px solid var(--border); border-radius: 6px; font: inherit; }

button {
  padding: 7px 14px;
  border: 1px solid var(--accent);
  background: var(--accent);
  color: #fff;
  border-radius: 6px;
  font: inherit;
  cursor: pointer;
}
button.danger { background: var(--danger); border-color: var(--danger); }
button:hover { opacity: 0.9; }

.table-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; }
th, td { padding: 8px 10px; text-align: left; border-bottom: 1px solid var(--border); vertical-align: top; }
th { background: #eef3f6; font-weight: 600; }
.actions { display: flex; gap: 6px; }

#message { max-width: 1100px; margin: 16px auto 0; padding: 10px 14px; border-radius: 6px; }
#message.ok { background: #e3f4ea; color: #1b5e3a; }
#message.error { background: #fbe4e1; color: #7a1f14; }
```

### `app/static/app.js`

```javascript
"use strict";

const $ = (selector) => document.querySelector(selector);
const STATUS = { scheduled: "Запланирован", completed: "Завершён", cancelled: "Отменён" };
const state = { doctors: [], patients: [] };

// ---------- Утилиты ----------

function el(tag, props = {}, ...children) {
  const node = Object.assign(document.createElement(tag), props);
  node.append(...children); // строки превращаются в текстовые узлы - XSS невозможен
  return node;
}

async function api(path, { method = "GET", body } = {}) {
  const response = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  if (response.status === 204) return null;
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(describeError(data));
  return data;
}

function describeError(data) {
  if (typeof data.detail === "string") return data.detail;
  if (Array.isArray(data.detail)) {
    return data.detail.map((e) => `${e.loc.slice(1).join(".")}: ${e.msg}`).join("; ");
  }
  return "Ошибка запроса";
}

function notify(text, isError = false) {
  const box = $("#message");
  box.textContent = text;
  box.className = isError ? "error" : "ok";
  box.hidden = false;
  clearTimeout(notify.timer);
  notify.timer = setTimeout(() => (box.hidden = true), 5000);
}

const guard = (handler) => async (...args) => {
  try {
    await handler(...args);
  } catch (error) {
    notify(error.message, true);
  }
};

const formatDateTime = (iso) =>
  new Date(iso).toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "short" });
const today = () => new Date().toLocaleDateString("sv-SE"); // YYYY-MM-DD по местному времени

function renderTable(table, headers, rows) {
  const head = el("thead", {}, el("tr", {}, ...headers.map((title) => el("th", { textContent: title }))));
  const body = el("tbody", {}, ...rows.map((cells) => el("tr", {}, ...cells.map((cell) => el("td", {}, cell)))));
  table.replaceChildren(head, body);
}

function fillSelect(select, items, label) {
  select.replaceChildren(...items.map((item) => el("option", { value: item.id, textContent: label(item) })));
}

async function removeItem(url, message, reload) {
  if (!confirm("Удалить запись?")) return;
  await api(url, { method: "DELETE" });
  notify(message);
  await reload();
}

// ---------- Справочники и свободные слоты ----------

async function loadDictionaries() {
  [state.doctors, state.patients] = await Promise.all([
    api("/api/doctors?limit=200"),
    api("/api/patients?limit=200"),
  ]);
  fillSelect($("#appt-doctor"), state.doctors, (d) => `${d.full_name} - ${d.specialty}`);
  fillSelect($("#appt-patient"), state.patients, (p) => p.full_name);
  await refreshSlots();
}

async function refreshSlots() {
  const doctorId = $("#appt-doctor").value;
  const day = $("#appt-day").value;
  const duration = $("#appt-duration").value;
  const select = $("#appt-slot");
  select.replaceChildren();
  if (!doctorId || !day) return;

  const data = await api(`/api/doctors/${doctorId}/slots?day=${day}&duration=${duration}`);
  if (data.slots.length === 0) {
    select.append(el("option", { value: "", textContent: "Нет свободных слотов" }));
    return;
  }
  for (const slot of data.slots) {
    select.append(el("option", { value: slot, textContent: slot.slice(11, 16) }));
  }
}

// ---------- Приёмы ----------

async function patchAppointment(id, body, message) {
  await api(`/api/appointments/${id}`, { method: "PATCH", body });
  notify(message);
  await loadAppointments();
}

async function finishAppointment(appointment) {
  const conclusion = prompt("Заключение врача:");
  if (conclusion === null) return;
  await patchAppointment(
    appointment.id,
    { status: "completed", conclusion: conclusion.trim() || null },
    "Приём завершён",
  );
}

function appointmentActions(appointment) {
  const box = el("span", { className: "actions" });
  if (appointment.status === "scheduled") {
    box.append(
      el("button", { textContent: "Завершить", onclick: guard(() => finishAppointment(appointment)) }),
      el("button", {
        textContent: "Отменить",
        className: "danger",
        onclick: guard(() => patchAppointment(appointment.id, { status: "cancelled" }, "Приём отменён")),
      }),
    );
  }
  return box;
}

async function loadAppointments() {
  await loadDictionaries();
  const params = new URLSearchParams({ limit: "200" });
  const day = $("#filter-day").value;
  if (day) params.set("day", day);
  const items = await api(`/api/appointments?${params}`);
  renderTable(
    $("#appt-table"),
    ["Время", "Пациент", "Врач", "Причина", "Статус", "Заключение", ""],
    items.map((a) => [
      formatDateTime(a.start_time),
      a.patient.full_name,
      `${a.doctor.full_name} (${a.doctor.specialty})`,
      a.reason,
      STATUS[a.status],
      a.conclusion ?? "-",
      appointmentActions(a),
    ]),
  );
}

// ---------- Врачи и пациенты ----------

async function loadDoctors() {
  await loadDictionaries();
  renderTable(
    $("#doctor-table"),
    ["ФИО", "Специальность", "Кабинет", ""],
    state.doctors.map((d) => [
      d.full_name,
      d.specialty,
      d.cabinet ?? "-",
      el("button", {
        textContent: "Удалить",
        className: "danger",
        onclick: guard(() => removeItem(`/api/doctors/${d.id}`, "Врач удалён", loadDoctors)),
      }),
    ]),
  );
}

async function loadPatients() {
  await loadDictionaries();
  renderTable(
    $("#patient-table"),
    ["ФИО", "Дата рождения", "Телефон", "Email", ""],
    state.patients.map((p) => [
      p.full_name,
      new Date(p.birth_date).toLocaleDateString("ru-RU"),
      p.phone,
      p.email ?? "-",
      el("button", {
        textContent: "Удалить",
        className: "danger",
        onclick: guard(() => removeItem(`/api/patients/${p.id}`, "Пациент удалён", loadPatients)),
      }),
    ]),
  );
}

// ---------- Вкладки ----------

const loaders = { appointments: loadAppointments, doctors: loadDoctors, patients: loadPatients };

function showTab(name) {
  document.querySelectorAll("nav button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll("main > section").forEach((s) => (s.hidden = s.id !== `tab-${name}`));
  return loaders[name]();
}

document.querySelectorAll("nav button").forEach((button) =>
  button.addEventListener("click", guard(() => showTab(button.dataset.tab))),
);

// ---------- Формы ----------

function bindCreateForm(selector, url, successMessage, reload) {
  $(selector).addEventListener(
    "submit",
    guard(async (event) => {
      event.preventDefault();
      const form = event.target;
      const data = Object.fromEntries(new FormData(form));
      for (const key of Object.keys(data)) {
        if (data[key] === "") delete data[key]; // пустые необязательные поля не отправляем
      }
      await api(url, { method: "POST", body: data });
      form.reset();
      notify(successMessage);
      await reload();
    }),
  );
}

bindCreateForm("#doctor-form", "/api/doctors", "Врач добавлен", loadDoctors);
bindCreateForm("#patient-form", "/api/patients", "Пациент добавлен", loadPatients);

$("#appt-form").addEventListener(
  "submit",
  guard(async (event) => {
    event.preventDefault();
    await api("/api/appointments", {
      method: "POST",
      body: {
        patient_id: Number($("#appt-patient").value),
        doctor_id: Number($("#appt-doctor").value),
        start_time: $("#appt-slot").value,
        duration_minutes: Number($("#appt-duration").value),
        reason: $("#appt-reason").value,
      },
    });
    $("#appt-reason").value = "";
    notify("Запись создана");
    await loadAppointments();
  }),
);

["#appt-doctor", "#appt-day", "#appt-duration"].forEach((selector) =>
  $(selector).addEventListener("change", guard(refreshSlots)),
);
$("#filter-day").addEventListener("change", guard(loadAppointments));
$("#filter-reset").addEventListener(
  "click",
  guard(async () => {
    $("#filter-day").value = "";
    await loadAppointments();
  }),
);

// ---------- Старт ----------

$("#appt-day").value = today();
guard(() => showTab("appointments"))();
```

## Тесты

Тесты работают с изолированной SQLite в памяти: зависимость `get_db` подменяется, поэтому файл `clinic.db` не затрагивается. Клиент создаётся без `with`, поэтому `lifespan` (создание таблиц и загрузка демо-данных в основную БД) не запускается.

### `tests/test_api.py`

```python
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app


@pytest.fixture()
def client():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    testing_session = sessionmaker(bind=engine, autoflush=False)

    def override_get_db():
        db = testing_session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()
    engine.dispose()


def tomorrow_at(hour: int, minute: int = 0) -> datetime:
    return (datetime.now() + timedelta(days=1)).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )


def make_doctor(client, name="Иванов Сергей Петрович", specialty="Терапевт"):
    response = client.post(
        "/api/doctors", json={"full_name": name, "specialty": specialty, "cabinet": "101"}
    )
    assert response.status_code == 201, response.text
    return response.json()


def make_patient(client, phone="+79990000001", name="Петров Пётр Петрович"):
    response = client.post(
        "/api/patients", json={"full_name": name, "birth_date": "1990-05-17", "phone": phone}
    )
    assert response.status_code == 201, response.text
    return response.json()


def book(client, doctor_id, patient_id, start, duration=30, reason="Консультация"):
    return client.post(
        "/api/appointments",
        json={
            "doctor_id": doctor_id,
            "patient_id": patient_id,
            "start_time": start.isoformat(),
            "duration_minutes": duration,
            "reason": reason,
        },
    )


def test_doctor_crud_and_case_insensitive_search(client):
    doctor = make_doctor(client)
    assert client.get(f"/api/doctors/{doctor['id']}").json()["specialty"] == "Терапевт"

    # поиск по кириллице без учёта регистра
    assert len(client.get("/api/doctors", params={"q": "иванов"}).json()) == 1
    assert len(client.get("/api/doctors", params={"q": "ИВАНОВ"}).json()) == 1
    assert client.get("/api/doctors", params={"q": "сидоров"}).json() == []

    response = client.patch(f"/api/doctors/{doctor['id']}", json={"cabinet": "202"})
    assert response.status_code == 200
    assert response.json()["cabinet"] == "202"

    assert client.delete(f"/api/doctors/{doctor['id']}").status_code == 204
    assert client.get(f"/api/doctors/{doctor['id']}").status_code == 404


def test_patient_phone_must_be_unique(client):
    make_patient(client)
    response = client.post(
        "/api/patients",
        json={"full_name": "Другой Человек", "birth_date": "1980-01-01", "phone": "+79990000001"},
    )
    assert response.status_code == 409


def test_patient_validation(client):
    bad_phone = client.post(
        "/api/patients",
        json={"full_name": "Тест Тестов", "birth_date": "1980-01-01", "phone": "abc"},
    )
    assert bad_phone.status_code == 422

    future_birth = client.post(
        "/api/patients",
        json={"full_name": "Тест Тестов", "birth_date": "2999-01-01", "phone": "+79990000009"},
    )
    assert future_birth.status_code == 422


def test_book_appointment(client):
    doctor, patient = make_doctor(client), make_patient(client)
    start = tomorrow_at(10)

    response = book(client, doctor["id"], patient["id"], start, duration=45)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "scheduled"
    assert body["end_time"] == (start + timedelta(minutes=45)).isoformat()
    assert body["patient"]["full_name"] == patient["full_name"]
    assert body["doctor"]["id"] == doctor["id"]


def test_unknown_doctor_or_patient(client):
    patient = make_patient(client)
    assert book(client, 999, patient["id"], tomorrow_at(10)).status_code == 404
    doctor = make_doctor(client)
    assert book(client, doctor["id"], 999, tomorrow_at(10)).status_code == 404


def test_doctor_double_booking_is_rejected(client):
    doctor = make_doctor(client)
    first, second = make_patient(client), make_patient(client, phone="+79990000002", name="Сидоров Иван")

    assert book(client, doctor["id"], first["id"], tomorrow_at(10)).status_code == 201
    # пересечение 10:15-10:45 с приёмом 10:00-10:30
    assert book(client, doctor["id"], second["id"], tomorrow_at(10, 15)).status_code == 409
    # приём "встык" разрешён
    assert book(client, doctor["id"], second["id"], tomorrow_at(10, 30)).status_code == 201


def test_patient_double_booking_is_rejected(client):
    first_doctor = make_doctor(client)
    second_doctor = make_doctor(client, name="Смирнова Ольга Викторовна", specialty="Кардиолог")
    patient = make_patient(client)

    assert book(client, first_doctor["id"], patient["id"], tomorrow_at(11)).status_code == 201
    assert book(client, second_doctor["id"], patient["id"], tomorrow_at(11, 15)).status_code == 409


def test_working_hours_and_past_time(client):
    doctor, patient = make_doctor(client), make_patient(client)

    assert book(client, doctor["id"], patient["id"], tomorrow_at(7, 30)).status_code == 422
    # 19:45 + 30 минут выходит за 20:00
    assert book(client, doctor["id"], patient["id"], tomorrow_at(19, 45)).status_code == 422
    assert book(client, doctor["id"], patient["id"], tomorrow_at(19, 30)).status_code == 201

    yesterday = datetime.now() - timedelta(days=1)
    assert book(client, doctor["id"], patient["id"], yesterday).status_code == 422


def test_free_slots_and_cancel_frees_slot(client):
    doctor, patient = make_doctor(client), make_patient(client)
    start = tomorrow_at(10)
    appointment = book(client, doctor["id"], patient["id"], start).json()

    def slots():
        response = client.get(
            f"/api/doctors/{doctor['id']}/slots", params={"day": start.date().isoformat()}
        )
        assert response.status_code == 200
        return response.json()["slots"]

    busy = slots()
    assert start.isoformat() not in busy
    assert tomorrow_at(9, 30).isoformat() in busy
    assert tomorrow_at(10, 30).isoformat() in busy

    response = client.patch(f"/api/appointments/{appointment['id']}", json={"status": "cancelled"})
    assert response.status_code == 200
    assert start.isoformat() in slots()


def test_reschedule_appointment(client):
    doctor, patient = make_doctor(client), make_patient(client)
    appointment = book(client, doctor["id"], patient["id"], tomorrow_at(10)).json()

    response = client.patch(
        f"/api/appointments/{appointment['id']}", json={"start_time": tomorrow_at(11).isoformat()}
    )
    assert response.status_code == 200, response.text
    assert response.json()["start_time"] == tomorrow_at(11).isoformat()
    assert response.json()["end_time"] == tomorrow_at(11, 30).isoformat()


def test_completed_appointment_is_final(client):
    doctor, patient = make_doctor(client), make_patient(client)
    appointment = book(client, doctor["id"], patient["id"], tomorrow_at(10)).json()
    url = f"/api/appointments/{appointment['id']}"

    done = client.patch(url, json={"status": "completed", "conclusion": "Здоров"})
    assert done.status_code == 200
    assert done.json()["conclusion"] == "Здоров"

    assert client.patch(url, json={"status": "cancelled"}).status_code == 409
    assert client.delete(url).status_code == 409


def test_cannot_delete_doctor_or_patient_with_appointments(client):
    doctor, patient = make_doctor(client), make_patient(client)
    book(client, doctor["id"], patient["id"], tomorrow_at(10))

    assert client.delete(f"/api/doctors/{doctor['id']}").status_code == 409
    assert client.delete(f"/api/patients/{patient['id']}").status_code == 409

    history = client.get(f"/api/patients/{patient['id']}/appointments")
    assert history.status_code == 200
    assert len(history.json()) == 1
```

## Запуск (Windows, PowerShell)

```powershell
cd clinic
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt

# запуск приложения
uvicorn app.main:app --reload

# запуск тестов
pytest -q
```

После запуска:

- веб-интерфейс: http://127.0.0.1:8000/
- Swagger UI: http://127.0.0.1:8000/docs
- ReDoc: http://127.0.0.1:8000/redoc

Файл БД `clinic.db` создаётся автоматически рядом с проектом. Чтобы вернуть демо-данные, достаточно удалить этот файл и перезапустить сервер.

## Таблица эндпоинтов

| Метод | URL | Описание | Коды |
|---|---|---|---|
| GET | `/api/health` | проверка работоспособности | 200 |
| GET | `/api/doctors` | список врачей (`specialty`, `q`, `skip`, `limit`) | 200 |
| POST | `/api/doctors` | добавить врача | 201, 422 |
| GET | `/api/doctors/{id}` | карточка врача | 200, 404 |
| PATCH | `/api/doctors/{id}` | изменить врача | 200, 404, 422 |
| DELETE | `/api/doctors/{id}` | удалить врача | 204, 404, 409 |
| GET | `/api/doctors/{id}/slots?day=YYYY-MM-DD&duration=30` | свободные слоты на день | 200, 404 |
| GET | `/api/patients` | список пациентов (`q`, `skip`, `limit`) | 200 |
| POST | `/api/patients` | зарегистрировать пациента | 201, 409, 422 |
| GET | `/api/patients/{id}` | карточка пациента | 200, 404 |
| PATCH | `/api/patients/{id}` | изменить пациента | 200, 404, 409, 422 |
| DELETE | `/api/patients/{id}` | удалить пациента | 204, 404, 409 |
| GET | `/api/patients/{id}/appointments` | история приёмов пациента | 200, 404 |
| GET | `/api/appointments` | список приёмов (`doctor_id`, `patient_id`, `day`, `status`) | 200 |
| POST | `/api/appointments` | записать на приём | 201, 404, 409, 422 |
| GET | `/api/appointments/{id}` | приём по id | 200, 404 |
| PATCH | `/api/appointments/{id}` | перенос, смена статуса, заключение | 200, 404, 409, 422 |
| DELETE | `/api/appointments/{id}` | удалить приём | 204, 404, 409 |

## Примеры запросов

Создание приёма через PowerShell (дату подставьте будущую, время в пределах 08:00-20:00):

```powershell
$body = @{
    patient_id       = 1
    doctor_id        = 1
    start_time       = "2026-10-12T10:00:00"
    duration_minutes = 30
    reason           = "Боль в горле"
} | ConvertTo-Json

Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/appointments `
    -ContentType "application/json; charset=utf-8" -Body ([Text.Encoding]::UTF8.GetBytes($body))
```

Свободные слоты врача:

```powershell
Invoke-RestMethod "http://127.0.0.1:8000/api/doctors/1/slots?day=2026-10-12&duration=30"
```

Завершение приёма:

```powershell
$patch = @{ status = "completed"; conclusion = "ОРВИ, назначено лечение" } | ConvertTo-Json
Invoke-RestMethod -Method Patch -Uri http://127.0.0.1:8000/api/appointments/1 `
    -ContentType "application/json; charset=utf-8" -Body ([Text.Encoding]::UTF8.GetBytes($patch))
```

Пример ответа при конфликте расписания (HTTP 409):

```json
{ "detail": "У врача уже есть приём в это время" }
```

## Принятые допущения и идеи для развития

Допущения:

- время во всех запросах местное, без часового пояса (запрос с часовым поясом отклоняется валидацией);
- часы работы фиксированные (08:00-20:00, ежедневно), шаг слотов 30 минут; значения лежат в `app/scheduling.py`;
- аутентификации нет: это учебный вариант, в котором все эндпоинты открыты;
- проверка пересечений выполняется запросом перед вставкой; для SQLite с одним писателем этого достаточно, а в боевой системе с PostgreSQL стоит добавить ограничение `EXCLUDE USING gist` или блокировку строки врача.

Что можно добавить дальше:

- аутентификацию и роли (регистратор, врач, администратор) через OAuth2 + JWT;
- миграции Alembic и переход на PostgreSQL;
- индивидуальные графики врачей, выходные и отпуска;
- напоминания пациентам по email или SMS;
- вывод расписания врача в виде календарной сетки;
- пагинацию с общим количеством записей в ответе и экспорт расписания в CSV.

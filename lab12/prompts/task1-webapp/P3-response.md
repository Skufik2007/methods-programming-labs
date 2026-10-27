### app/schemas_extra.py

```python
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
```

### app/routers/common.py

```python
"""Общие зависимости и типы параметров для новых роутеров.

* ``get_now`` не определяется заново, а берётся из ``app.routers.appointments``: тогда один
  ``app.dependency_overrides[get_now]`` в тестах действует на все роутеры сразу;
* ``Annotated``-псевдонимы ролей (``AdminUser``, ``StaffUser`` ...) - грубый фильтр по роли;
  права на уровне объектов проверяет сервисный слой.
"""

from datetime import date, datetime
from typing import Annotated

from fastapi import Depends, Path, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import User, UserRole
from app.routers.appointments import get_now
from app.security import require_roles

MAX_ID = 2_147_483_647  # граница INTEGER в PostgreSQL (как EntityId в схемах)
MIN_QUERY_DATE = date(1970, 1, 1)  # разумные границы дат в параметрах: защита от OverflowError
MAX_QUERY_DATE = date(2100, 12, 31)

SessionDep = Annotated[AsyncSession, Depends(get_session)]
NowDep = Annotated[datetime, Depends(get_now)]

AdminUser = Annotated[User, Depends(require_roles(UserRole.ADMIN))]
StaffUser = Annotated[User, Depends(require_roles(UserRole.ADMIN, UserRole.REGISTRAR))]
DoctorUser = Annotated[User, Depends(require_roles(UserRole.DOCTOR))]
PatientUser = Annotated[User, Depends(require_roles(UserRole.PATIENT))]
# Медицинские данные: administrative staff (admin, registrar) получает 403 уже на этом уровне.
MedicalReaderUser = Annotated[User, Depends(require_roles(UserRole.DOCTOR, UserRole.PATIENT))]

IdPath = Annotated[int, Path(gt=0, le=MAX_ID)]
LimitQuery = Annotated[int, Query(ge=1, le=100)]
OffsetQuery = Annotated[int, Query(ge=0, le=MAX_ID)]
SearchQuery = Annotated[str | None, Query(min_length=1, max_length=100)]
RequiredDate = Annotated[date, Query(ge=MIN_QUERY_DATE, le=MAX_QUERY_DATE)]
```

### app/services/dbutil.py

```python
"""Общие помощники слоя БД: коммит с превращением гонки в 409 и поиск без учёта регистра.

``ci_contains`` / ``ci_equals`` сравнивают через ``lower()``. В PostgreSQL ``lower()`` корректно
работает с кириллицей, если база в UTF-8 и локаль не ``C``. Встроенный ``lower()`` SQLite
понимает только ASCII, поэтому для SQLite (тесты) при каждом подключении регистрируется
Python-версия ``lower``: иначе поиск «иванов» не находил бы «Иванов». Обработчик повешен
на класс ``Engine``, так что действует и на движки, которые тесты создают сами. Если
зарегистрировать функцию не удалось, поиск продолжает работать, но регистр игнорируется
только для ASCII.
"""

from typing import Any

from sqlalchemy import ColumnElement, String, event, func
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from app.errors import ConflictError, is_unique_violation


def _py_lower(value: str | None) -> str | None:
    """Unicode-версия SQL-функции ``lower`` для SQLite."""
    return None if value is None else value.lower()


@event.listens_for(Engine, "connect")
def _register_sqlite_unicode_lower(dbapi_connection: Any, _connection_record: Any) -> None:
    """Подменяет встроенную ASCII-версию ``lower`` в соединениях SQLite (sqlite3 и aiosqlite)."""
    if "sqlite" not in type(dbapi_connection).__module__:
        return
    try:
        dbapi_connection.create_function("lower", 1, _py_lower, deterministic=True)
    except Exception:  # noqa: BLE001 - без регистрации остаётся ASCII-версия, это не авария
        return


def ci_contains(column: InstrumentedAttribute[str], needle: str) -> ColumnElement[bool]:
    """``column`` содержит ``needle`` без учёта регистра; ``%`` и ``_`` в ``needle`` экранируются."""
    return func.lower(column, type_=String).contains(needle.lower(), autoescape=True)


def ci_equals(column: InstrumentedAttribute[str], value: str) -> ColumnElement[bool]:
    """``column`` равна ``value`` без учёта регистра."""
    return func.lower(column, type_=String) == value.lower()


async def commit_or_conflict(session: AsyncSession, conflict_message: str) -> None:
    """Фиксирует транзакцию; нарушение уникальности (гонка за unique-индекс) превращает в 409.

    Любая другая ``IntegrityError`` (внешний ключ, CHECK) - это баг, а не конфликт клиента,
    и пробрасывается как есть.
    """
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        if is_unique_violation(exc):
            raise ConflictError(conflict_message) from exc
        raise
```

### app/services/users.py

```python
"""Управление пользователями (только администратор): список, создание, изменение.

Защита от потери доступа к системе: администратор не может снять с себя роль ``admin`` и
деактивировать себя (403: тот же запрос от другого администратора допустим).

Согласованность роли и профилей: роль нельзя сменить, если у пользователя есть профиль
``Doctor`` или ``Patient``, не соответствующий новой роли (409). Строка пользователя
блокируется (``FOR UPDATE``), поэтому одновременное создание профиля врача/пациента и смена
роли выполняются по очереди и не оставляют «врача с ролью patient».

Токены уже выданных сессий после смены роли и деактивации отклоняются самим
``get_current_user`` (сверка роли и ``is_active`` с БД).
"""

from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import BusinessRuleError, ConflictError, NotFoundError, PermissionDeniedError
from app.models import Doctor, Patient, User, UserRole
from app.schemas import UserCreate, UserUpdate
from app.security import hash_password_async
from app.services.dbutil import ci_contains, commit_or_conflict


@dataclass(frozen=True, slots=True)
class UserFilters:
    """Фильтры списка пользователей; ``q`` - подстрока email или ФИО без учёта регистра."""

    role: UserRole | None = None
    is_active: bool | None = None
    q: str | None = None


def ensure_admin(user: User) -> None:
    """Операции над пользователями и профилями врачей доступны только администратору."""
    if user.role != UserRole.ADMIN:
        raise PermissionDeniedError("Insufficient permissions: administrator role required")


async def lock_user(session: AsyncSession, user_id: int) -> User:
    """Читает пользователя и блокирует его строку до конца транзакции; нет такого - 404.

    ``populate_existing`` нужен, чтобы роль и профиль проверялись по свежим данным БД,
    а не по объекту, уже лежащему в сессии.
    """
    stmt = select(User).where(User.id == user_id).with_for_update().execution_options(populate_existing=True)
    user = await session.scalar(stmt)
    if user is None:
        raise NotFoundError("User not found")
    return user


async def list_users(
    session: AsyncSession, actor: User, filters: UserFilters, *, limit: int, offset: int
) -> list[User]:
    """Список пользователей с фильтрами и пагинацией; порядок - по ``id`` (стабильная пагинация)."""
    ensure_admin(actor)
    stmt = select(User)
    if filters.role is not None:
        stmt = stmt.where(User.role == filters.role)
    if filters.is_active is not None:
        stmt = stmt.where(User.is_active.is_(filters.is_active))
    term = (filters.q or "").strip()
    if term:
        stmt = stmt.where(or_(ci_contains(User.email, term), ci_contains(User.full_name, term)))
    stmt = stmt.order_by(User.id).limit(limit).offset(offset)
    return list((await session.scalars(stmt)).all())


async def create_user(session: AsyncSession, actor: User, data: UserCreate) -> User:
    """Создаёт активного пользователя с любой ролью. Профиль врача/пациента создаётся отдельно."""
    ensure_admin(actor)
    if await session.scalar(select(User.id).where(User.email == data.email)) is not None:
        raise ConflictError("Email is already registered")

    password_hash = await hash_password_async(data.password.get_secret_value())
    user = User(
        email=data.email,
        password_hash=password_hash,
        full_name=data.full_name,
        role=data.role,
        is_active=True,
    )
    session.add(user)
    await commit_or_conflict(session, "Email is already registered")
    return user


async def _ensure_role_fits_profiles(session: AsyncSession, user_id: int, new_role: UserRole) -> None:
    """Новая роль не должна противоречить существующим профилям пользователя (409)."""
    if new_role != UserRole.DOCTOR:
        if await session.scalar(select(Doctor.id).where(Doctor.user_id == user_id)) is not None:
            raise ConflictError(f"The user has a doctor profile; the role cannot be changed to '{new_role.value}'")
    if new_role != UserRole.PATIENT:
        if await session.scalar(select(Patient.id).where(Patient.user_id == user_id)) is not None:
            raise ConflictError(f"The user is linked to a patient; the role cannot be changed to '{new_role.value}'")


async def update_user(session: AsyncSession, actor: User, user_id: int, data: UserUpdate) -> User:
    """PATCH: ФИО, email, пароль, роль, ``is_active``.

    * пустое тело - 422; неизвестный пользователь - 404;
    * администратор не может снять роль ``admin`` с себя и деактивировать себя - 403;
    * смена роли при несоответствующем профиле ``Doctor`` / ``Patient`` - 409;
    * занятый email - 409.
    """
    ensure_admin(actor)
    if not data.model_fields_set:
        raise BusinessRuleError("No fields to update")

    user = await lock_user(session, user_id)

    if user.id == actor.id:
        if data.role is not None and data.role != UserRole.ADMIN:
            raise PermissionDeniedError("Administrators cannot remove their own admin role")
        if data.is_active is False:
            raise PermissionDeniedError("Administrators cannot deactivate themselves")

    if data.role is not None and data.role != user.role:
        await _ensure_role_fits_profiles(session, user.id, data.role)

    if data.email is not None and data.email != user.email:
        taken = await session.scalar(select(User.id).where(User.email == data.email, User.id != user.id))
        if taken is not None:
            raise ConflictError("Email is already registered")
        user.email = data.email
    if data.full_name is not None:
        user.full_name = data.full_name
    if data.role is not None:
        user.role = data.role
    if data.is_active is not None:
        user.is_active = data.is_active
    if data.password is not None:
        user.password_hash = await hash_password_async(data.password.get_secret_value())

    await commit_or_conflict(session, "Email is already registered")
    return user
```

### app/routers/users.py

```python
"""Роуты пользователей: список, создание, изменение. Только администратор.

Роль проверяется через ``AdminUser`` (403 остальным), запреты на уровне объектов
(нельзя снять с себя роль admin, несоответствие профилям) - в ``app.services.users``.
"""

from typing import Annotated

from fastapi import APIRouter, Query, status

from app.models import User, UserRole
from app.routers.common import AdminUser, IdPath, LimitQuery, OffsetQuery, SearchQuery, SessionDep
from app.schemas import UserCreate, UserOut, UserUpdate
from app.services import users as service
from app.services.users import UserFilters

router = APIRouter(prefix="/api", tags=["users"])


@router.get("/users", response_model=list[UserOut])
async def list_users(
    user: AdminUser,
    session: SessionDep,
    role: Annotated[UserRole | None, Query()] = None,
    is_active: Annotated[bool | None, Query()] = None,
    q: SearchQuery = None,
    limit: LimitQuery = 20,
    offset: OffsetQuery = 0,
) -> list[User]:
    """Список пользователей; ``q`` - подстрока email или ФИО без учёта регистра."""
    filters = UserFilters(role=role, is_active=is_active, q=q)
    return await service.list_users(session, user, filters, limit=limit, offset=offset)


@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def create_user(data: UserCreate, user: AdminUser, session: SessionDep) -> User:
    """Создаёт пользователя с любой ролью."""
    return await service.create_user(session, user, data)


@router.patch("/users/{user_id}", response_model=UserOut)
async def update_user(user_id: IdPath, data: UserUpdate, user: AdminUser, session: SessionDep) -> User:
    """Меняет ФИО, email, пароль, роль, ``is_active``."""
    return await service.update_user(session, user, user_id, data)
```

### app/services/doctors.py

```python
"""Профили врачей: список, чтение, создание, изменение графика.

* Читать врачей могут все аутентифицированные; в списке нет врачей с деактивированным
  аккаунтом, но прямой запрос по id возвращает любого (у пациента могут остаться записи).
* Создаёт и меняет профили только администратор.
* Смена графика (дни, смена, длина слота) не должна оставлять будущие записи ``scheduled``
  вне смены или вне сетки. Проверка использует те же чистые функции ``scheduling``, что и
  создание записи, поэтому правила не расходятся. Строка врача блокируется тем же способом,
  что при записи на приём, так что параллельная запись не пройдёт по старому графику.

ФИО врача лежит у пользователя; ответы собираются одним JOIN-запросом (без N+1).
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.errors import BusinessRuleError, ConflictError, NotFoundError
from app.models import Appointment, AppointmentStatus, Doctor, User, UserRole
from app.schemas import DoctorCreate, DoctorUpdate
from app.services import scheduling
from app.services.appointments import _lock_doctor, _schedule_of  # noqa: PLC2701 - единый протокол блокировок
from app.services.dbutil import ci_equals, commit_or_conflict
from app.services.users import ensure_admin, lock_user


@dataclass(frozen=True, slots=True)
class DoctorView:
    """Профиль врача и ФИО его пользователя."""

    doctor: Doctor
    full_name: str


async def list_doctors(session: AsyncSession, *, specialty: str | None) -> list[DoctorView]:
    """Врачи с активным аккаунтом; ``specialty`` - точное совпадение без учёта регистра.

    Сортировка по ФИО, затем по ``id``. Один запрос с JOIN.
    """
    stmt = (
        select(Doctor, User.full_name)
        .select_from(Doctor)
        .join(User, User.id == Doctor.user_id)
        .where(User.is_active.is_(True))
    )
    term = (specialty or "").strip()
    if term:
        stmt = stmt.where(ci_equals(Doctor.specialty, term))
    rows = await session.execute(stmt.order_by(User.full_name, Doctor.id))
    return [DoctorView(doctor=doctor, full_name=full_name) for doctor, full_name in rows.all()]


async def get_doctor(session: AsyncSession, doctor_id: int) -> DoctorView:
    """Врач по id (в том числе с деактивированным аккаунтом); нет такого - 404."""
    stmt = (
        select(Doctor, User.full_name)
        .select_from(Doctor)
        .join(User, User.id == Doctor.user_id)
        .where(Doctor.id == doctor_id)
    )
    row = (await session.execute(stmt)).first()
    if row is None:
        raise NotFoundError("Doctor not found")
    return DoctorView(doctor=row[0], full_name=row[1])


async def create_doctor(session: AsyncSession, actor: User, data: DoctorCreate) -> DoctorView:
    """Создаёт профиль врача для пользователя с ролью ``doctor``, у которого профиля ещё нет.

    Нет пользователя - 404; роль не ``doctor`` - 422; профиль уже есть - 409.
    """
    ensure_admin(actor)
    user = await lock_user(session, data.user_id)  # сериализация со сменой роли
    if user.role != UserRole.DOCTOR:
        raise BusinessRuleError("The user must have the 'doctor' role")
    if await session.scalar(select(Doctor.id).where(Doctor.user_id == user.id)) is not None:
        raise ConflictError("The user already has a doctor profile")

    doctor = Doctor(
        user_id=user.id,
        specialty=data.specialty,
        cabinet=data.cabinet,
        slot_minutes=data.slot_minutes,
        work_start=data.work_start,
        work_end=data.work_end,
        work_days=list(data.work_days),
    )
    session.add(doctor)
    await commit_or_conflict(session, "The user already has a doctor profile")
    return DoctorView(doctor=doctor, full_name=user.full_name)


async def _count_incompatible_appointments(
    session: AsyncSession, doctor_id: int, schedule: scheduling.Schedule, *, now: datetime
) -> int:
    """Сколько будущих записей ``scheduled`` врача не укладывается в смену или сетку ``schedule``.

    Из БД читаются только два столбца времени; правила применяются теми же функциями
    ``scheduling.validate_within_shift`` / ``validate_slot_grid``, что и при записи
    (в том числе «длительность - от 1 до 4 слотов»). «Будущая» - начало строго после ``now``.
    """
    tz = get_settings().clinic_tz
    rows = await session.execute(
        select(Appointment.starts_at, Appointment.ends_at).where(
            Appointment.doctor_id == doctor_id,
            Appointment.status == AppointmentStatus.SCHEDULED,
            Appointment.starts_at > now,
        )
    )
    incompatible = 0
    for starts_at, ends_at in rows.all():
        try:
            scheduling.validate_within_shift(starts_at, ends_at, schedule, tz)
            scheduling.validate_slot_grid(starts_at, ends_at, schedule, tz)
        except scheduling.SchedulingError:
            incompatible += 1
    return incompatible


async def update_doctor(
    session: AsyncSession, actor: User, doctor_id: int, data: DoctorUpdate, *, now: datetime
) -> DoctorView:
    """PATCH: кабинет, специальность, график.

    * пустое тело - 422; нет врача - 404;
    * ``work_start >= work_end`` после слияния с сохранёнными значениями - 422;
    * если график (дни, смена, длина слота) меняется и из-за этого ``N`` будущих записей
      ``scheduled`` оказываются вне смены или вне сетки - 409 с числом ``N`` в сообщении,
      график остаётся прежним. Смена только кабинета/специальности записи не проверяет.
    """
    ensure_admin(actor)
    if not data.model_fields_set:
        raise BusinessRuleError("No fields to update")

    doctor = await _lock_doctor(session, doctor_id)

    new_start = data.work_start if data.work_start is not None else doctor.work_start
    new_end = data.work_end if data.work_end is not None else doctor.work_end
    if new_start >= new_end:
        raise BusinessRuleError("work_start must be earlier than work_end (overnight shifts are not supported)")
    new_schedule = scheduling.Schedule(
        work_days=frozenset(data.work_days if data.work_days is not None else doctor.work_days),
        work_start=new_start,
        work_end=new_end,
        slot_minutes=data.slot_minutes if data.slot_minutes is not None else doctor.slot_minutes,
    )

    if new_schedule != _schedule_of(doctor):
        incompatible = await _count_incompatible_appointments(session, doctor.id, new_schedule, now=now)
        if incompatible:
            raise ConflictError(
                f"The new schedule leaves {incompatible} upcoming scheduled appointment(s) outside the shift "
                "or the slot grid; reschedule or cancel them first"
            )

    if data.specialty is not None:
        doctor.specialty = data.specialty
    if data.cabinet is not None:
        doctor.cabinet = data.cabinet
    if data.slot_minutes is not None:
        doctor.slot_minutes = data.slot_minutes
    if data.work_start is not None:
        doctor.work_start = data.work_start
    if data.work_end is not None:
        doctor.work_end = data.work_end
    if data.work_days is not None:
        doctor.work_days = list(data.work_days)  # JSON-колонку присваиваем целиком

    full_name = await session.scalar(select(User.full_name).where(User.id == doctor.user_id))
    await commit_or_conflict(session, "The doctor was modified concurrently; please retry")
    return DoctorView(doctor=doctor, full_name=full_name or "")
```

### app/routers/doctors.py

```python
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
```

### app/services/patients.py

```python
"""Пациенты: поиск, чтение, создание, изменение и привязка аккаунта.

Права на уровне объектов:

* admin / registrar - любой пациент; список, создание и изменение - только они;
* doctor - только пациент, у которого есть хотя бы одна запись к этому врачу в любом
  статусе, кроме ``cancelled``; иначе 404 (чужой пациент неотличим от несуществующего);
* patient - только свой профиль (``/me`` или собственный id; чужой id - 404).

Условие видимости входит в SQL-запрос, поэтому чужие строки не читаются.

Привязка аккаунта (``user_id``): пользователь должен существовать (404), иметь роль
``patient`` (422) и не быть привязанным к другому пациенту (409).
"""

from sqlalchemy import ColumnElement, false, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import BusinessRuleError, ConflictError, NotFoundError, PermissionDeniedError
from app.models import Appointment, AppointmentStatus, Patient, User, UserRole
from app.schemas import PatientCreate, PatientUpdate
from app.services.appointments import STAFF_ROLES, Actor, load_actor
from app.services.dbutil import ci_contains, commit_or_conflict
from app.services.users import lock_user

_POLICY_CONFLICT = "A patient with this policy number already exists"
_RACE_CONFLICT = "Policy number is already in use or the user is already linked to another patient"


def _ensure_staff(user: User) -> None:
    """Список, создание и изменение пациентов - только admin / registrar."""
    if user.role not in STAFF_ROLES:
        raise PermissionDeniedError("Insufficient permissions to manage patients")


def _visibility_clause(actor: Actor) -> ColumnElement[bool] | None:
    """SQL-условие «какие пациенты пользователю видны»; ``None`` - ограничений нет."""
    if actor.role in STAFF_ROLES:
        return None
    if actor.role == UserRole.DOCTOR and actor.doctor_id is not None:
        return (
            select(Appointment.id)
            .where(
                Appointment.patient_id == Patient.id,
                Appointment.doctor_id == actor.doctor_id,
                Appointment.status != AppointmentStatus.CANCELLED,
            )
            .correlate(Patient)
            .exists()
        )
    if actor.role == UserRole.PATIENT and actor.patient_id is not None:
        return Patient.id == actor.patient_id
    return false()


async def list_patients(
    session: AsyncSession, user: User, q: str | None, *, limit: int, offset: int
) -> list[Patient]:
    """Список пациентов (admin / registrar); ``q`` - подстрока ФИО (без учёта регистра) или номера полиса."""
    _ensure_staff(user)
    stmt = select(Patient)
    term = (q or "").strip()
    if term:
        stmt = stmt.where(
            or_(ci_contains(Patient.full_name, term), Patient.policy_number.contains(term, autoescape=True))
        )
    stmt = stmt.order_by(Patient.full_name, Patient.id).limit(limit).offset(offset)
    return list((await session.scalars(stmt)).all())


async def get_patient(session: AsyncSession, user: User, patient_id: int) -> Patient:
    """Пациент по id, если он виден пользователю; иначе 404."""
    actor = await load_actor(session, user)
    stmt = select(Patient).where(Patient.id == patient_id)
    clause = _visibility_clause(actor)
    if clause is not None:
        stmt = stmt.where(clause)
    patient = await session.scalar(stmt)
    if patient is None:
        raise NotFoundError("Patient not found")
    return patient


async def get_own_patient(session: AsyncSession, user: User) -> Patient:
    """Профиль пациента текущего пользователя (роль ``patient``); профиля нет - 404."""
    if user.role != UserRole.PATIENT:
        raise PermissionDeniedError("Only patients have a personal patient profile")
    patient = await session.scalar(select(Patient).where(Patient.user_id == user.id))
    if patient is None:
        raise NotFoundError("Patient profile not found")
    return patient


async def _check_user_link(session: AsyncSession, user_id: int, *, exclude_patient_id: int | None) -> None:
    """Пользователя можно привязать к пациенту: он есть, имеет роль patient и не занят другим пациентом."""
    user = await lock_user(session, user_id)  # блокировка сериализует привязку со сменой роли
    if user.role != UserRole.PATIENT:
        raise BusinessRuleError("The linked user must have the 'patient' role")
    stmt = select(Patient.id).where(Patient.user_id == user.id)
    if exclude_patient_id is not None:
        stmt = stmt.where(Patient.id != exclude_patient_id)
    if await session.scalar(stmt) is not None:
        raise ConflictError("The user is already linked to another patient")


async def create_patient(session: AsyncSession, user: User, data: PatientCreate) -> Patient:
    """Создаёт пациента (admin / registrar). Занятый полис - 409; ``user_id`` проверяется как при привязке."""
    _ensure_staff(user)
    if await session.scalar(select(Patient.id).where(Patient.policy_number == data.policy_number)) is not None:
        raise ConflictError(_POLICY_CONFLICT)
    if data.user_id is not None:
        await _check_user_link(session, data.user_id, exclude_patient_id=None)

    patient = Patient(
        user_id=data.user_id,
        full_name=data.full_name,
        birth_date=data.birth_date,
        phone=data.phone,
        policy_number=data.policy_number,
    )
    session.add(patient)
    await commit_or_conflict(session, _RACE_CONFLICT)
    return patient


async def update_patient(session: AsyncSession, user: User, patient_id: int, data: PatientUpdate) -> Patient:
    """PATCH пациента (admin / registrar), в том числе привязка и отвязка аккаунта.

    ``user_id: null`` отвязывает аккаунт, ``phone: null`` стирает телефон. Пустое тело - 422.
    """
    _ensure_staff(user)
    fields = data.model_fields_set
    if not fields:
        raise BusinessRuleError("No fields to update")

    stmt = select(Patient).where(Patient.id == patient_id).with_for_update().execution_options(populate_existing=True)
    patient = await session.scalar(stmt)
    if patient is None:
        raise NotFoundError("Patient not found")

    if data.policy_number is not None and data.policy_number != patient.policy_number:
        taken = await session.scalar(
            select(Patient.id).where(Patient.policy_number == data.policy_number, Patient.id != patient.id)
        )
        if taken is not None:
            raise ConflictError(_POLICY_CONFLICT)
        patient.policy_number = data.policy_number
    if "user_id" in fields:
        if data.user_id is not None and data.user_id != patient.user_id:
            await _check_user_link(session, data.user_id, exclude_patient_id=patient.id)
        patient.user_id = data.user_id
    if data.full_name is not None:
        patient.full_name = data.full_name
    if data.birth_date is not None:
        patient.birth_date = data.birth_date
    if "phone" in fields:
        patient.phone = data.phone

    await commit_or_conflict(session, _RACE_CONFLICT)
    return patient
```

### app/routers/patients.py

```python
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
    user: StaffUser,
    session: SessionDep,
    q: SearchQuery = None,
    limit: LimitQuery = 20,
    offset: OffsetQuery = 0,
) -> list[Patient]:
    """Поиск пациентов (admin, registrar): ``q`` - подстрока ФИО или номера полиса."""
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
```

### app/services/medical.py

```python
"""Медицинские данные: назначения (рецепты) и записи медицинской карты.

Доступ строже, чем к расписанию:

* читать: врач - данные пациентов, у которых он вёл приём (у него есть запись в статусе
  ``completed``; правило уровня пациента, поэтому врач видит и то, что назначили другие
  врачи); пациент - только свои; admin и registrar - никогда (403: они работают с
  расписанием, а не с диагнозами);
* недоступный объект неотличим от несуществующего (404); 403 - административному персоналу
  и операциям, на которые у видимого объекта нет права (чужая запись медкарты);
* назначение создаёт только врач этой записи, запись должна быть ``completed``; изменения и
  удаления назначений нет (выписанный рецепт не меняется);
* запись медкарты создаёт только врач; ``doctor_id`` берётся из профиля текущего врача (если
  тело его содержит, значение должно совпасть); править её может только автор и только в течение
  24 часов после создания; удаления нет.

Слой владеет транзакцией (``commit()`` здесь). Время - aware UTC; «сейчас» приходит параметром.
"""

from datetime import datetime, timedelta

from sqlalchemy import ColumnElement, false, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute, aliased

from app.errors import BusinessRuleError, ConflictError, NotFoundError, PermissionDeniedError
from app.models import Appointment, AppointmentStatus, MedicalRecord, Patient, Prescription, User, UserRole
from app.schemas import MedicalRecordCreate, MedicalRecordUpdate, PrescriptionCreate
from app.services.appointments import STAFF_ROLES, Actor, load_actor

RECORD_EDIT_WINDOW = timedelta(hours=24)  # автор правит запись не позже чем через 24 часа (ровно 24 часа - можно)


def _ensure_not_staff(actor: Actor) -> None:
    """Административный персонал не читает медицинские данные (403)."""
    if actor.role in STAFF_ROLES:
        raise PermissionDeniedError("Medical data is not available to administrative staff")


def _require_doctor_id(actor: Actor) -> int:
    """id профиля врача для операций записи; не врач или нет профиля - 403."""
    if actor.role != UserRole.DOCTOR:
        raise PermissionDeniedError("Only doctors can perform this operation")
    if actor.doctor_id is None:
        raise PermissionDeniedError("Doctor profile not found")
    return actor.doctor_id


def _readable_patient_clause(actor: Actor, patient_column: InstrumentedAttribute[int]) -> ColumnElement[bool]:
    """SQL-условие «данные этого пациента пользователю доступны» для столбца ``patient_column``.

    Подзапрос строится на ``aliased(Appointment)``: иначе при внешнем запросе по ``appointments``
    SQLAlchemy автоматически «скоррелировал» бы подзапрос и убрал его FROM.
    """
    if actor.role == UserRole.PATIENT and actor.patient_id is not None:
        return patient_column == actor.patient_id
    if actor.role == UserRole.DOCTOR and actor.doctor_id is not None:
        treated = aliased(Appointment)
        return patient_column.in_(
            select(treated.patient_id).where(
                treated.doctor_id == actor.doctor_id,
                treated.status == AppointmentStatus.COMPLETED,
            )
        )
    return false()


async def _doctor_treated_patient(session: AsyncSession, doctor_id: int, patient_id: int) -> bool:
    """Вёл ли врач хотя бы один приём этого пациента (запись в статусе ``completed``)."""
    stmt = select(Appointment.id).where(
        Appointment.doctor_id == doctor_id,
        Appointment.patient_id == patient_id,
        Appointment.status == AppointmentStatus.COMPLETED,
    )
    return await session.scalar(stmt.limit(1)) is not None


# --------------------------------------------------------------------------- #
# Назначения (рецепты)
# --------------------------------------------------------------------------- #


async def create_prescription(
    session: AsyncSession, user: User, data: PrescriptionCreate, *, now: datetime
) -> Prescription:
    """Выписывает назначение: только врач этой записи (чужая запись - 404), запись ``completed`` (иначе 409)."""
    actor = await load_actor(session, user)
    doctor_id = _require_doctor_id(actor)

    appointment = await session.scalar(
        select(Appointment).where(Appointment.id == data.appointment_id, Appointment.doctor_id == doctor_id)
    )
    if appointment is None:
        raise NotFoundError("Appointment not found")
    if appointment.status != AppointmentStatus.COMPLETED:
        raise ConflictError("Prescriptions can only be issued for completed appointments")

    prescription = Prescription(
        appointment_id=appointment.id,
        medication=data.medication,
        dosage=data.dosage,
        instructions=data.instructions,
        issued_at=now,
    )
    session.add(prescription)
    await session.commit()
    return prescription


async def list_prescriptions(session: AsyncSession, user: User, appointment_id: int) -> list[Prescription]:
    """Назначения по записи, если данные её пациента пользователю доступны.

    admin / registrar - 403; недоступная или несуществующая запись - 404.
    """
    actor = await load_actor(session, user)
    _ensure_not_staff(actor)

    visible = await session.scalar(
        select(Appointment.id).where(
            Appointment.id == appointment_id,
            _readable_patient_clause(actor, Appointment.patient_id),
        )
    )
    if visible is None:
        raise NotFoundError("Appointment not found")

    stmt = (
        select(Prescription)
        .where(Prescription.appointment_id == appointment_id)
        .order_by(Prescription.issued_at, Prescription.id)
    )
    return list((await session.scalars(stmt)).all())


# --------------------------------------------------------------------------- #
# Медицинская карта
# --------------------------------------------------------------------------- #


async def create_medical_record(
    session: AsyncSession, user: User, data: MedicalRecordCreate, *, now: datetime
) -> MedicalRecord:
    """Создаёт запись медкарты от имени текущего врача.

    * ``data.doctor_id`` должен совпадать с профилем текущего врача, иначе 403;
    * указан ``appointment_id``: запись должна принадлежать этому врачу (иначе 404) и этому
      пациенту (иначе 422) и быть ``completed`` (иначе 409);
    * не указан: у врача должен быть хотя бы один ``completed``-приём этого пациента, иначе
      404 (пациент, которого врач не вёл, для него неотличим от несуществующего).
    """
    actor = await load_actor(session, user)
    doctor_id = _require_doctor_id(actor)
    if data.doctor_id != doctor_id:
        raise PermissionDeniedError("doctor_id must match the current doctor")

    if data.appointment_id is not None:
        appointment = await session.scalar(
            select(Appointment).where(Appointment.id == data.appointment_id, Appointment.doctor_id == doctor_id)
        )
        if appointment is None:
            raise NotFoundError("Appointment not found")
        if appointment.patient_id != data.patient_id:
            raise BusinessRuleError("The appointment belongs to a different patient")
        if appointment.status != AppointmentStatus.COMPLETED:
            raise ConflictError("Medical records can only be linked to completed appointments")
    elif not await _doctor_treated_patient(session, doctor_id, data.patient_id):
        raise NotFoundError("Patient not found")

    record = MedicalRecord(
        patient_id=data.patient_id,
        doctor_id=doctor_id,
        appointment_id=data.appointment_id,
        diagnosis_code=data.diagnosis_code,
        notes=data.notes,
        created_at=now,
    )
    session.add(record)
    await session.commit()
    return record


async def list_medical_records(
    session: AsyncSession, user: User, patient_id: int, *, limit: int, offset: int
) -> list[MedicalRecord]:
    """Записи медкарты пациента, новые первыми (``created_at`` по убыванию, затем ``id``).

    admin / registrar - 403; недоступный или несуществующий пациент - 404.
    """
    actor = await load_actor(session, user)
    _ensure_not_staff(actor)

    visible = await session.scalar(
        select(Patient.id).where(Patient.id == patient_id, _readable_patient_clause(actor, Patient.id))
    )
    if visible is None:
        raise NotFoundError("Patient not found")

    stmt = (
        select(MedicalRecord)
        .where(MedicalRecord.patient_id == patient_id)
        .order_by(MedicalRecord.created_at.desc(), MedicalRecord.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list((await session.scalars(stmt)).all())


async def update_medical_record(
    session: AsyncSession, user: User, record_id: int, data: MedicalRecordUpdate, *, now: datetime
) -> MedicalRecord:
    """PATCH записи медкарты: только автор и только в течение 24 часов после создания.

    * не врач - 403; пустое тело - 422;
    * записи нет или пациент врачу недоступен - 404;
    * запись видна врачу, но написана не им - 403;
    * прошло больше 24 часов - 403 (как окно пациента в записях: право истекло по времени).
    """
    actor = await load_actor(session, user)
    doctor_id = _require_doctor_id(actor)
    fields = data.model_fields_set
    if not fields:
        raise BusinessRuleError("No fields to update")

    stmt = select(MedicalRecord).where(MedicalRecord.id == record_id).with_for_update()
    record = await session.scalar(stmt.execution_options(populate_existing=True))
    if record is None:
        raise NotFoundError("Medical record not found")
    if record.doctor_id != doctor_id:
        if await _doctor_treated_patient(session, doctor_id, record.patient_id):
            raise PermissionDeniedError("Only the author can modify a medical record")
        raise NotFoundError("Medical record not found")
    if now - record.created_at > RECORD_EDIT_WINDOW:
        raise PermissionDeniedError("A medical record can only be modified within 24 hours after creation")

    if "diagnosis_code" in fields:
        record.diagnosis_code = data.diagnosis_code  # type: ignore[assignment]  # None запрещён схемой
    if "notes" in fields:
        record.notes = data.notes
    await session.commit()
    return record
```

### app/routers/prescriptions.py

```python
"""Роуты назначений (рецептов): выписать и посмотреть по записи.

Редактирования и удаления нет: выписанный рецепт не меняется. admin и registrar получают 403
на обоих эндпоинтах (медицинские данные), чужая запись для врача/пациента - 404.
"""

from fastapi import APIRouter, status

from app.models import Prescription
from app.routers.common import DoctorUser, IdPath, MedicalReaderUser, NowDep, SessionDep
from app.schemas import PrescriptionCreate, PrescriptionOut
from app.services import medical as service

router = APIRouter(prefix="/api", tags=["prescriptions"])


@router.post("/prescriptions", response_model=PrescriptionOut, status_code=status.HTTP_201_CREATED)
async def create_prescription(
    data: PrescriptionCreate, user: DoctorUser, session: SessionDep, now: NowDep
) -> Prescription:
    """Выписывает назначение: врач этой записи, запись в статусе completed."""
    return await service.create_prescription(session, user, data, now=now)


@router.get("/appointments/{appointment_id}/prescriptions", response_model=list[PrescriptionOut])
async def list_prescriptions(
    appointment_id: IdPath, user: MedicalReaderUser, session: SessionDep
) -> list[Prescription]:
    """Назначения по записи (врач, который вёл пациента; сам пациент)."""
    return await service.list_prescriptions(session, user, appointment_id)
```

### app/routers/medical_records.py

```python
"""Роуты медицинской карты: создать запись, прочитать карту пациента, поправить свою запись.

Удаления нет. admin и registrar получают 403. ``doctor_id`` в теле обязателен по схеме
``MedicalRecordCreate``, но значимо только его совпадение с профилем текущего врача.
"""

from fastapi import APIRouter, status

from app.models import MedicalRecord
from app.routers.common import DoctorUser, IdPath, LimitQuery, MedicalReaderUser, NowDep, OffsetQuery, SessionDep
from app.schemas import MedicalRecordCreate, MedicalRecordOut, MedicalRecordUpdate
from app.services import medical as service

router = APIRouter(prefix="/api", tags=["medical-records"])


@router.post("/medical-records", response_model=MedicalRecordOut, status_code=status.HTTP_201_CREATED)
async def create_medical_record(
    data: MedicalRecordCreate, user: DoctorUser, session: SessionDep, now: NowDep
) -> MedicalRecord:
    """Создаёт запись медкарты от имени текущего врача."""
    return await service.create_medical_record(session, user, data, now=now)


@router.get("/patients/{patient_id}/medical-records", response_model=list[MedicalRecordOut])
async def list_medical_records(
    patient_id: IdPath,
    user: MedicalReaderUser,
    session: SessionDep,
    limit: LimitQuery = 50,
    offset: OffsetQuery = 0,
) -> list[MedicalRecord]:
    """Медкарта пациента, новые записи первыми."""
    return await service.list_medical_records(session, user, patient_id, limit=limit, offset=offset)


@router.patch("/medical-records/{record_id}", response_model=MedicalRecordOut)
async def update_medical_record(
    record_id: IdPath, data: MedicalRecordUpdate, user: DoctorUser, session: SessionDep, now: NowDep
) -> MedicalRecord:
    """Правка записи: только автор и только в течение 24 часов после создания."""
    return await service.update_medical_record(session, user, record_id, data, now=now)
```

### app/services/analytics.py

```python
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


async def top_diagnoses(
    session: AsyncSession, date_from: date, date_to: date, limit: int
) -> list[DiagnosisCountOut]:
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
```

### app/routers/analytics.py

```python
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
```

### app/main.py

```python
"""Фабрика приложения FastAPI.

``create_app()`` собирает приложение: подключает роутеры и обработчики доменных ошибок,
а в ``lifespan`` закрывает пул соединений с БД при остановке. Запуск:
``uvicorn app.main:app`` (или ``uvicorn --factory app.main:create_app``).
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.db import dispose_engine
from app.errors import register_exception_handlers
from app.routers import analytics as analytics_router
from app.routers import appointments as appointments_router
from app.routers import auth as auth_router
from app.routers import doctors as doctors_router
from app.routers import medical_records as medical_records_router
from app.routers import patients as patients_router
from app.routers import prescriptions as prescriptions_router
from app.routers import users as users_router


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Жизненный цикл приложения: при остановке освобождает пул соединений."""
    try:
        yield
    finally:
        await dispose_engine()


def create_app() -> FastAPI:
    """Создаёт и настраивает экземпляр приложения."""
    app = FastAPI(title="Clinic management system", version="1.0.0", lifespan=lifespan)
    register_exception_handlers(app)
    app.include_router(auth_router.router)
    app.include_router(appointments_router.router)
    app.include_router(users_router.router)
    app.include_router(doctors_router.router)
    app.include_router(patients_router.router)
    app.include_router(prescriptions_router.router)
    app.include_router(medical_records_router.router)
    app.include_router(analytics_router.router)

    @app.get("/api/health", tags=["system"])
    async def health() -> dict[str, str]:
        """Проверка живости сервиса (без обращения к БД)."""
        return {"status": "ok"}

    return app


app = create_app()
```

---

## Допущения

1. Коды ошибок там, где задание их не фиксирует: администратор меняет себя (роль/деактивация) - 403; пустой PATCH - 422 (как в записях); неизвестный пользователь в теле - 404; пользователь не той роли при создании профиля врача или привязке к пациенту - 422; профиль уже есть / пользователь уже привязан - 409; приём не `completed` при выписке рецепта или привязке записи медкарты - 409; `appointment_id` другого пациента - 422; право на правку записи медкарты истекло через 24 часа - 403 (как окно пациента в 2 часа; ровно 24 часа - ещё можно).
2. Доступ врача к медданным - буквальное чтение задания, на уровне пациента: врач, проводивший хотя бы один `completed`-приём пациента, видит все его рецепты и все записи медкарты, в том числе чужие. Недоступный или несуществующий пациент/запись - 404; 403 получают admin/registrar и сосед-врач, пытающийся править чужую (видимую ему) запись.
3. `MedicalRecordCreate.doctor_id` по существующей схеме обязателен, схему менять нельзя; значит, клиент всегда присылает id своего профиля врача, при несовпадении - 403. Врач без профиля `Doctor`: чтение - 404, запись - 403. Для пациента, которого врач не вёл (запись без `appointment_id`), отвечаем 404, чтобы не раскрывать существование пациента.
4. `GET /api/doctors/{id}` отдаёт и деактивированного врача, в списке его нет. Список врачей без пагинации (задание её не требует); фильтр `specialty` - точное совпадение без учёта регистра. Список записей медкарты пагинируется (`limit` по умолчанию 50, максимум 100), чтобы длинная история не отдавалась целиком.
5. Пациент может читать свой профиль и по `/api/patients/me`, и по собственному id; чужой id - 404.
6. Аналитика: записи относятся к периоду по `starts_at`, диагнозы - по `created_at` записи медкарты; в `appointments-per-day` считаются записи всех статусов; доли при нулевом знаменателе равны 0.0 (не `null`); `available_minutes` - длина смены × число рабочих дней (неполный последний слот учитывается); `load_percent` не ограничен 100; в `doctors-load` попадают все врачи (с флагом `is_active`), ФИО не выводится (только `doctor_id` и специальность), чтобы не выдавать персональные данные в агрегате.
7. Смена пароля не отзывает выданные токены (в `security.py` нет версии токена); смена роли и деактивация отзывают их автоматически через сверку с БД.
8. Добавлены два вспомогательных файла сверх перечисленных: `app/routers/common.py` (зависимости и типы параметров) и `app/services/dbutil.py` (коммит с 409 и регистронезависимый поиск). `get_now` импортируется из `app.routers.appointments`, чтобы единая подмена в тестах работала везде. Существующие файлы, кроме `app/main.py`, не менялись; тестов нет.

## Что проверить на ревью особенно внимательно

1. `app/services/dbutil.py`: обработчик `connect` на классе `Engine` подменяет `lower()` в SQLite через `create_function` (в том числе через адаптер aiosqlite). Нужно убедиться, что функция реально регистрируется (поиск «иванов» находит «Иванов» в тестах) и что в PostgreSQL кириллица работает (UTF-8 база, не локаль `C`).
2. `app/services/analytics.py`, `appointments_per_day`: две реализации «местной даты» (PostgreSQL `timezone()`; SQLite - сдвиг по отрезкам постоянного смещения, у границы перевода часов это приближение). Проверить на обеих СУБД, граничные записи около местной полуночи и группировку через подзапрос.
3. `analytics._duration_seconds` и суммы в `doctors_load`: `EXTRACT(EPOCH ...)` в PostgreSQL возвращает numeric (`Decimal`), в SQLite - `julianday`; приведение к `float` и округление до минут. Нужен интеграционный прогон на PostgreSQL.
4. `update_doctor`: проверка будущих записей идёт в Python по чистым функциям `scheduling` (читаются только два столбца), строка врача блокируется через `_lock_doctor`; граница «будущая» - `starts_at > now`; запись длиннее 4 новых слотов тоже считается нарушением; `_lock_doctor` и `_schedule_of` импортируются из `services.appointments` как приватные.
5. Матрица доступа в `services/medical.py`: `_readable_patient_clause` (подзапрос на `aliased(Appointment)` против автокорреляции), порядок проверок 403/404/409/422 в `create_medical_record` и `update_medical_record`, 24-часовое окно и сравнение `now - created_at`.
6. `update_user`: защита от самоблокировки (сравнение `user.id == actor.id` после `populate_existing`), проверка соответствия роли профилям, блокировка строки пользователя для сериализации с `POST /api/doctors` и привязкой пациента.
7. `routers/patients.py`: маршрут `/patients/me` обязан идти раньше `/patients/{patient_id}`; видимость пациента для врача (любая запись кроме `cancelled`) отличается от доступа к медданным (только `completed`) - это намеренно.
8. Индексы: аналитика фильтрует `appointments.starts_at` и `medical_records.created_at` без ведущего индекса по этим столбцам (составной индекс начинается с `doctor_id`); на больших объёмах понадобятся индексы. Малые значения `count` в `top-diagnoses` за короткий период могут косвенно указывать на конкретного человека.

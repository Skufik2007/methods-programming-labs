Из `app/` изменён один файл — `app/errors.py` (причина — в заметке под его заголовком). Остальной код приложения не тронут. Файлы приведены целиком; дополнительно добавлены `scripts/__init__.py` (пакет для `python -m scripts.seed`) и необязательный тест `tests/test_exclusion_violation.py` к изменению в `app/errors.py`.

### alembic.ini

```ini
# Alembic configuration.
#
# NOTE: keep this file ASCII-only. Alembic reads it with the locale encoding, so Cyrillic
# comments would break `alembic` on Windows (cp1251 cannot decode some UTF-8 sequences).
#
# The database URL is NOT set here: migrations/env.py takes it from app.config.get_settings()
# (DATABASE_URL environment variable or the .env file), so no password ends up in the repository.
#
# Run every command from the project root. DATABASE_URL and JWT_SECRET must be set
# (JWT_SECRET only because Settings validates both values at once):
#
#   alembic upgrade head                           # apply all migrations
#   alembic check                                  # does the DB schema match the models? (exit 0 = yes)
#   alembic downgrade base                         # revert everything
#   alembic revision --autogenerate -m "message"   # draft of a new migration
#   alembic upgrade head --sql                     # print SQL only (offline mode)

[alembic]
script_location = migrations
prepend_sys_path = .
# Alembic >= 1.16: path separator for prepend_sys_path; older versions simply ignore the option.
path_separator = os
file_template = %%(rev)s_%%(slug)s

# Logging
[loggers]
keys = root,sqlalchemy,alembic

[handlers]
keys = console

[formatters]
keys = generic

[logger_root]
level = WARNING
handlers = console
qualname =

[logger_sqlalchemy]
level = WARNING
handlers =
qualname = sqlalchemy.engine

[logger_alembic]
level = INFO
handlers =
qualname = alembic

[handler_console]
class = StreamHandler
args = (sys.stderr,)
level = NOTSET
formatter = generic

[formatter_generic]
format = %(levelname)-5.5s [%(name)s] %(message)s
datefmt = %H:%M:%S
```

### migrations/env.py

```python
"""Окружение Alembic: асинхронные миграции (SQLAlchemy 2.0 + asyncpg / aiosqlite).

* URL базы берётся из ``app.config.get_settings()`` (``DATABASE_URL``), а не из alembic.ini:
  одни и те же настройки у приложения и у миграций, пароль не попадает в репозиторий;
* ``target_metadata = Base.metadata`` - из неё же ``alembic revision --autogenerate`` и
  ``alembic check`` вычисляют отличия; имена ограничений и индексов задаёт naming convention
  из ``app/models.py``;
* ``compare_type=True``: изменение типа или длины столбца считается отличием схемы;
* для SQLite ``render_as_batch=True``: ``ALTER TABLE`` там ограничен, и автогенерация
  оформляет изменения как batch-операции (пересоздание таблицы).
"""

import asyncio
from logging.config import fileConfig
from typing import Any

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings
from app.models import Base

config = context.config

if config.config_file_name is not None:
    # disable_existing_loggers=False: иначе fileConfig отключил бы логгеры приложения.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata

# Объекты, которые есть только в БД и намеренно не описаны в моделях (SQLite EXCLUDE не поддерживает):
# ограничение EXCLUDE из 0001_initial.py и создаваемый им GiST-индекс с тем же именем.
# Имя должно совпадать с EXCLUDE_NAME в migrations/versions/0001_initial.py.
DB_ONLY_OBJECT_NAMES: frozenset[str] = frozenset({"ex_appointments_doctor_no_overlap"})


def get_database_url() -> str:
    """URL базы из настроек приложения (``DATABASE_URL``); ошибка настроек останавливает Alembic."""
    return get_settings().database_url.get_secret_value()


def include_object(_obj: Any, name: str | None, _type: str, reflected: bool, _compare_to: Any) -> bool:
    """Исключает из сравнения объекты, существующие только в БД (см. ``DB_ONLY_OBJECT_NAMES``).

    Без этого фильтра ``alembic check`` мог бы счесть индекс, на котором держится EXCLUDE,
    «лишним» и предложить его удалить.
    """
    return not (reflected and name in DB_ONLY_OBJECT_NAMES)


def run_migrations_offline() -> None:
    """Offline-режим (``alembic upgrade head --sql``): без подключения, SQL уходит в stdout."""
    url = get_database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_as_batch=make_url(url).get_backend_name() == "sqlite",
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Выполняет миграции на синхронном «срезе» асинхронного соединения."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        render_as_batch=connection.dialect.name == "sqlite",
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Online-режим: отдельный engine без пула (процесс миграции короткий), миграции через ``run_sync``."""
    connectable = create_async_engine(get_database_url(), poolclass=pool.NullPool)
    try:
        async with connectable.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await connectable.dispose()


def run_migrations_online() -> None:
    """Точка входа online-режима: запускает event loop и асинхронные миграции."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
```

### migrations/script.py.mako

```mako
"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
${imports if imports else ""}

# Идентификаторы ревизий, используемые Alembic.
revision: str = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    """Применяет миграцию."""
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    """Откатывает миграцию."""
    ${downgrades if downgrades else "pass"}
```

### migrations/versions/0001_initial.py

```python
"""Начальная схема клиники.

Revision ID: 0001
Revises:
Create Date: 2026-10-10

Таблицы: users, doctors, patients, appointments, prescriptions, medical_records.

Схема в точности повторяет ``app/models.py``: имена ограничений и индексов построены по
naming convention из ``Base.metadata`` (в миграции они записаны явно через ``op.f``, поэтому
не зависят от настроек env.py), enum-поля - это VARCHAR(16) + CHECK, моменты времени -
``TIMESTAMP WITH TIME ZONE``. Проверка соответствия: ``alembic upgrade head && alembic check``.

Только для PostgreSQL дополнительно создаётся то, чего нет в моделях (SQLite этого не умеет):

* расширение ``btree_gist``;
* ограничение EXCLUDE: у одного врача не может быть двух неотменённых записей с пересекающимися
  интервалами ``[starts_at, ends_at)``. Частичный уникальный индекс (doctor_id, starts_at)
  ловит только записи с одинаковым началом, EXCLUDE - любое пересечение. Нарушение даёт
  SQLSTATE 23P01, которое ``app.errors.is_unique_violation`` превращает в HTTP 409.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Идентификаторы ревизий, используемые Alembic.
revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Имя ограничения EXCLUDE; то же значение указано в DB_ONLY_OBJECT_NAMES в migrations/env.py.
EXCLUDE_NAME = "ex_appointments_doctor_no_overlap"

# Предикат частичного индекса и EXCLUDE: «неотменённая» запись (как _ACTIVE_APPOINTMENT в models.py).
ACTIVE_APPOINTMENT = "status <> 'cancelled'"


def _is_postgresql() -> bool:
    """Идёт ли миграция на PostgreSQL (работает и в offline-режиме ``--sql``)."""
    return op.get_context().dialect.name == "postgresql"


def upgrade() -> None:
    """Создаёт таблицы, индексы и (на PostgreSQL) защиту от пересечения записей врача."""
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(length=254), nullable=False),
        sa.Column("password_hash", sa.String(length=128), nullable=False),
        sa.Column("full_name", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("token_version", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("role IN ('admin', 'registrar', 'doctor', 'patient')", name=op.f("ck_users_user_role")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("email", name=op.f("uq_users_email")),
    )

    op.create_table(
        "doctors",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("specialty", sa.String(length=100), nullable=False),
        sa.Column("cabinet", sa.String(length=20), nullable=False),
        sa.Column("slot_minutes", sa.SmallInteger(), server_default=sa.text("30"), nullable=False),
        sa.Column("work_start", sa.Time(), nullable=False),
        sa.Column("work_end", sa.Time(), nullable=False),
        sa.Column("work_days", sa.JSON(), nullable=False),
        sa.CheckConstraint("slot_minutes BETWEEN 5 AND 240", name=op.f("ck_doctors_slot_minutes_range")),
        sa.CheckConstraint("work_start < work_end", name=op.f("ck_doctors_shift_order")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_doctors_user_id_users"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_doctors")),
        sa.UniqueConstraint("user_id", name=op.f("uq_doctors_user_id")),
    )

    op.create_table(
        "patients",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("full_name", sa.String(length=255), nullable=False),
        sa.Column("birth_date", sa.Date(), nullable=False),
        sa.Column("phone", sa.String(length=16), nullable=True),
        sa.Column("policy_number", sa.String(length=16), nullable=False),
        sa.CheckConstraint("length(policy_number) = 16", name=op.f("ck_patients_policy_number_length")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_patients_user_id_users"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_patients")),
        sa.UniqueConstraint("policy_number", name=op.f("uq_patients_policy_number")),
        sa.UniqueConstraint("user_id", name=op.f("uq_patients_user_id")),
    )

    op.create_table(
        "appointments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("doctor_id", sa.Integer(), nullable=False),
        sa.Column("patient_id", sa.Integer(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="scheduled", nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('scheduled', 'completed', 'cancelled', 'no_show')",
            name=op.f("ck_appointments_appointment_status"),
        ),
        sa.CheckConstraint("ends_at > starts_at", name=op.f("ck_appointments_ends_after_starts")),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_appointments_created_by_users"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["doctor_id"], ["doctors.id"], name=op.f("fk_appointments_doctor_id_doctors"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["patient_id"], ["patients.id"], name=op.f("fk_appointments_patient_id_patients"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_appointments")),
    )
    op.create_index("ix_appointments_doctor_id_starts_at", "appointments", ["doctor_id", "starts_at"])
    op.create_index("ix_appointments_patient_id", "appointments", ["patient_id"])
    op.create_index("ix_appointments_status", "appointments", ["status"])
    # Врач не может иметь две неотменённые записи на одно и то же время начала.
    op.create_index(
        "uq_appointments_doctor_id_starts_at_active",
        "appointments",
        ["doctor_id", "starts_at"],
        unique=True,
        postgresql_where=sa.text(ACTIVE_APPOINTMENT),
        sqlite_where=sa.text(ACTIVE_APPOINTMENT),
    )

    op.create_table(
        "prescriptions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("appointment_id", sa.Integer(), nullable=False),
        sa.Column("medication", sa.String(length=255), nullable=False),
        sa.Column("dosage", sa.String(length=100), nullable=False),
        sa.Column("instructions", sa.Text(), nullable=True),
        sa.Column("issued_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["appointment_id"],
            ["appointments.id"],
            name=op.f("fk_prescriptions_appointment_id_appointments"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_prescriptions")),
    )
    op.create_index("ix_prescriptions_appointment_id", "prescriptions", ["appointment_id"])

    op.create_table(
        "medical_records",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("patient_id", sa.Integer(), nullable=False),
        sa.Column("doctor_id", sa.Integer(), nullable=False),
        sa.Column("appointment_id", sa.Integer(), nullable=True),
        sa.Column("diagnosis_code", sa.String(length=6), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "length(diagnosis_code) BETWEEN 3 AND 6", name=op.f("ck_medical_records_diagnosis_code_length")
        ),
        sa.ForeignKeyConstraint(
            ["appointment_id"],
            ["appointments.id"],
            name=op.f("fk_medical_records_appointment_id_appointments"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["doctor_id"], ["doctors.id"], name=op.f("fk_medical_records_doctor_id_doctors"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["patient_id"], ["patients.id"], name=op.f("fk_medical_records_patient_id_patients"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_medical_records")),
    )
    op.create_index("ix_medical_records_patient_id", "medical_records", ["patient_id"])
    op.create_index("ix_medical_records_doctor_id", "medical_records", ["doctor_id"])
    op.create_index("ix_medical_records_appointment_id", "medical_records", ["appointment_id"])

    if _is_postgresql():
        # btree_gist позволяет объединить в одном GiST-индексе равенство по скаляру (doctor_id)
        # и пересечение диапазонов. CREATE EXTENSION требует права CREATE на базу (в PostgreSQL 13+
        # btree_gist - «доверенное» расширение, суперпользователь не нужен).
        op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
        op.execute(
            f"ALTER TABLE appointments ADD CONSTRAINT {EXCLUDE_NAME} "
            "EXCLUDE USING gist (doctor_id WITH =, tstzrange(starts_at, ends_at, '[)') WITH &&) "
            f"WHERE ({ACTIVE_APPOINTMENT})"
        )


def downgrade() -> None:
    """Удаляет всё, что создал ``upgrade``: ограничение EXCLUDE, таблицы (вместе с индексами), расширение."""
    if _is_postgresql():
        op.execute(f"ALTER TABLE appointments DROP CONSTRAINT IF EXISTS {EXCLUDE_NAME}")

    # Порядок обратен созданию: сначала таблицы, ссылающиеся на другие. Индексы удаляются вместе с таблицами.
    op.drop_table("medical_records")
    op.drop_table("prescriptions")
    op.drop_table("appointments")
    op.drop_table("patients")
    op.drop_table("doctors")
    op.drop_table("users")

    if _is_postgresql():
        # Расширение можно удалить только после таблиц: от его классов операторов зависел EXCLUDE.
        op.execute("DROP EXTENSION IF EXISTS btree_gist")
```

### app/errors.py

Причина изменения: `is_unique_violation` распознавал только SQLSTATE `23505`, а нарушение EXCLUDE в PostgreSQL — это `23P01` (`exclusion_violation`). Без правки `IntegrityError` от EXCLUDE не классифицировался как конфликт, пробрасывался из `_commit` / `commit_or_conflict` и давал 500. Теперь множество «конфликтных» SQLSTATE — `{23505, 23P01}`. Имя функции оставлено, чтобы не трогать три её вызова (`app/services/appointments.py::_commit`, `app/services/dbutil.py::commit_or_conflict`, `app/routers/auth.py::register`); 23P01 возможен только на `appointments`, поэтому в двух других местах поведение не меняется. Других отличий от исходного файла нет.

```python
"""Доменные исключения и их преобразование в HTTP-ответы.

Сервисный слой бросает исключения, ничего не зная про HTTP; обработчик, зарегистрированный
функцией ``register_exception_handlers``, превращает их в ответ ``{"detail": "..."}``:

* ``NotFoundError``         - 404 (объект не найден или скрыт от текущего пользователя);
* ``ConflictError``         - 409 (конфликт с текущим состоянием: занятое время, неверный переход статуса);
* ``PermissionDeniedError`` - 403 (операция запрещена роли или владельцу);
* ``BusinessRuleError``     - 422 (нарушено бизнес-правило: прошлое время, вне смены, не по сетке).

Также здесь лежит ``is_unique_violation`` - классификация ``IntegrityError`` (PostgreSQL / SQLite):
нарушение уникальности и (только PostgreSQL) нарушение ограничения EXCLUDE.
"""

from typing import ClassVar

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError

_PG_UNIQUE_VIOLATION = "23505"
# exclusion_violation: ограничение EXCLUDE из миграции 0001 (пересечение неотменённых записей врача).
_PG_EXCLUSION_VIOLATION = "23P01"
_PG_CONFLICT_SQLSTATES: frozenset[str] = frozenset({_PG_UNIQUE_VIOLATION, _PG_EXCLUSION_VIOLATION})
# SQLITE_CONSTRAINT_PRIMARYKEY, SQLITE_CONSTRAINT_UNIQUE
_SQLITE_UNIQUE_CODES: frozenset[int] = frozenset({1555, 2067})


class DomainError(Exception):
    """Базовое доменное исключение: ``detail`` - текст для клиента, ``status_code`` - HTTP-статус."""

    status_code: ClassVar[int] = 500

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class NotFoundError(DomainError):
    """Объект не найден (или скрыт от текущего пользователя - снаружи это неразличимо)."""

    status_code = 404


class ConflictError(DomainError):
    """Запрос конфликтует с текущим состоянием данных."""

    status_code = 409


class PermissionDeniedError(DomainError):
    """У пользователя нет права на операцию."""

    status_code = 403


class BusinessRuleError(DomainError):
    """Запрос синтаксически корректен, но нарушает бизнес-правило."""

    status_code = 422


def is_unique_violation(exc: IntegrityError) -> bool:
    """Возвращает ``True``, если ``IntegrityError`` вызван нарушением уникальности (или EXCLUDE).

    Нужна, чтобы превращать в 409 только гонки за уникальный индекс, а не любые ошибки
    целостности (нарушение внешнего ключа или CHECK - это баг, а не конфликт клиента).

    * PostgreSQL (asyncpg): SQLSTATE ``23505`` (unique_violation) либо ``23P01``
      (exclusion_violation - ограничение EXCLUDE, запрещающее пересечение записей врача;
      по смыслу это та же «уникальность», только для диапазонов); значение лежит в атрибуте
      ``sqlstate`` / ``pgcode`` у ``exc.orig``;
    * SQLite: ``sqlite_errorcode`` 2067 / 1555 (Python 3.11+) либо текст ``UNIQUE constraint failed``.
    """
    for error in (exc.orig, getattr(exc.orig, "__cause__", None)):
        if error is None:
            continue
        sqlstates = (getattr(error, "sqlstate", None), getattr(error, "pgcode", None))
        if any(code in _PG_CONFLICT_SQLSTATES for code in sqlstates):
            return True
        if getattr(error, "sqlite_errorcode", None) in _SQLITE_UNIQUE_CODES:
            return True
    return "UNIQUE constraint failed" in str(exc.orig)


async def _handle_domain_error(_request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, DomainError):  # недостижимо: обработчик зарегистрирован только для DomainError
        raise exc
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


def register_exception_handlers(app: FastAPI) -> None:
    """Регистрирует обработчик доменных ошибок.

    Starlette ищет обработчик по MRO исключения, поэтому одного обработчика на базовый
    ``DomainError`` достаточно для всех четырёх подклассов.
    """
    app.add_exception_handler(DomainError, _handle_domain_error)
```

### tests/test_exclusion_violation.py

Необязательный тест к изменению в `app/errors.py`: сам EXCLUDE в SQLite не воспроизвести, поэтому проверяется классификация SQLSTATE и превращение в `ConflictError`. Настоящую гонку на PostgreSQL проверяет шаг «EXCLUDE constraint rejects overlapping appointments» в CI.

```python
"""SQLSTATE 23P01 (ограничение EXCLUDE) даёт 409, как и 23505; прочие ошибки целостности - нет.

Ограничение EXCLUDE создаёт миграция 0001 только в PostgreSQL, поэтому сама гонка здесь не
воспроизводится: тест проверяет классификацию ошибки и превращение её в ``ConflictError``.
"""

from typing import cast

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ConflictError, is_unique_violation
from app.services.dbutil import commit_or_conflict


class _PgError(Exception):
    """Имитация исключения драйвера PostgreSQL: SQLSTATE лежит в атрибутах ``sqlstate`` и ``pgcode``."""

    def __init__(self, sqlstate: str) -> None:
        super().__init__(f"SQLSTATE {sqlstate}")
        self.sqlstate = sqlstate
        self.pgcode = sqlstate


class _FailingSession:
    """Заглушка сессии: ``commit`` падает с заданной ошибкой, ``rollback`` запоминает вызов."""

    def __init__(self, error: IntegrityError) -> None:
        self._error = error
        self.rolled_back = False

    async def commit(self) -> None:
        """Всегда падает: имитирует нарушение ограничения в момент фиксации транзакции."""
        raise self._error

    async def rollback(self) -> None:
        """Запоминает, что транзакция была откачена."""
        self.rolled_back = True


def _integrity_error(sqlstate: str) -> IntegrityError:
    """``IntegrityError`` так, как его строит SQLAlchemy: исходное исключение драйвера лежит в ``orig``."""
    return IntegrityError("INSERT INTO appointments ...", {}, _PgError(sqlstate))


@pytest.mark.parametrize("sqlstate", ["23505", "23P01"])
def test_conflict_sqlstates_are_classified(sqlstate: str) -> None:
    """unique_violation и exclusion_violation - конфликт конкурентных запросов."""
    assert is_unique_violation(_integrity_error(sqlstate))


@pytest.mark.parametrize("sqlstate", ["23502", "23503", "23514"])  # NOT NULL, FOREIGN KEY, CHECK
def test_other_integrity_errors_are_not_conflicts(sqlstate: str) -> None:
    """Остальные нарушения целостности - баг, а не конфликт клиента: 409 они не дают."""
    assert not is_unique_violation(_integrity_error(sqlstate))


async def test_exclusion_violation_becomes_conflict() -> None:
    """Гонка, пойманная EXCLUDE, превращается в ConflictError (то есть в HTTP 409), а не в 500."""
    session = _FailingSession(_integrity_error("23P01"))
    with pytest.raises(ConflictError, match="slot taken"):
        await commit_or_conflict(cast(AsyncSession, session), "slot taken")
    assert session.rolled_back


async def test_foreign_key_violation_is_not_swallowed() -> None:
    """Нарушение внешнего ключа пробрасывается как есть (будет 500 - это ошибка в коде)."""
    session = _FailingSession(_integrity_error("23503"))
    with pytest.raises(IntegrityError):
        await commit_or_conflict(cast(AsyncSession, session), "slot taken")
    assert session.rolled_back
```

### scripts/__init__.py

```python
"""Вспомогательные скрипты проекта (запускаются как ``python -m scripts.<имя>``)."""
```

### scripts/seed.py

```python
"""Демо-данные для показа системы управления клиникой.

Запуск (из корня проекта; ``DATABASE_URL`` и ``JWT_SECRET`` заданы, миграции применены)::

    SEED_PASSWORD='<пароль демо-пользователей>' python -m scripts.seed            # bash / zsh
    $env:SEED_PASSWORD='<пароль>'; python -m scripts.seed                         # PowerShell
    python -m scripts.seed --now 2026-10-12T09:00:00+03:00                        # зафиксировать «сегодня»

В Docker (``docker compose up -d`` уже выполнен; пароль берётся из окружения хоста)::

    export SEED_PASSWORD='<пароль демо-пользователей>'
    docker compose run --rm --no-deps -e SEED_PASSWORD app python -m scripts.seed

Что создаётся: администратор, регистратор, 4 врача (разные специальности, графики и длина слота),
20 пациентов (у троих есть учётная запись), записи на приём за последние 30 дней
(completed / no_show / cancelled) и на 14 дней вперёд (scheduled), записи медкарты с кодами МКБ-10
и рецепты к части завершённых приёмов. Все персонажи вымышленные, назначения лекарств - только
иллюстрация и не являются медицинскими рекомендациями.

Свойства:

* идемпотентность: пользователи ищутся по email, пациенты - по номеру полиса, профили врачей - по
  пользователю; история записей создаётся только если у демо-врачей ещё нет ни одной записи, поэтому
  повторный запуск (в том числе в другой день) ничего не дублирует и не меняет;
* пароль всех демо-пользователей берётся из переменной окружения ``SEED_PASSWORD`` (значения по
  умолчанию нет); в конце печатаются логины, но не пароль;
* детерминированность: генератор случайных чисел инициализирован константой ``RNG_SEED``; при одном и
  том же «текущем моменте» (``--now``) на пустой базе набор данных одинаков (кроме хэшей паролей:
  у bcrypt случайная соль); даты записей отсчитываются от «сегодня», поэтому в другой день набор
  сдвигается вместе с календарём;
* все записи проходят проверки ``app.services.scheduling`` (рабочий день, смена, сетка слотов,
  окно бронирования) и не пересекаются ни у врача, ни у пациента (в том числе отменённые).
"""

import argparse
import asyncio
import os
import random
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import SessionFactory, engine
from app.models import (
    Appointment,
    AppointmentStatus,
    Doctor,
    MedicalRecord,
    Patient,
    Prescription,
    User,
    UserRole,
)
from app.schemas import PASSWORD_MAX_BYTES, PASSWORD_MIN_LENGTH
from app.security import hash_password_async
from app.services import scheduling

RNG_SEED = 42  # фиксированное зерно генератора случайных чисел (подобрано: нагрузка по дням недели ровная)
EMAIL_DOMAIN = "clinic.example"  # зарезервированный TLD (RFC 2606): адреса заведомо не настоящие

HISTORY_DAYS = 30  # записи за последние N дней
FUTURE_DAYS = 14  # и на N дней вперёд
FUTURE_MARGIN = timedelta(hours=1)  # будущая запись начинается не раньше чем через час после «сейчас»
APPOINTMENTS_PER_DAY_PAST = (1, 4)  # записей у врача в рабочий день: от и до включительно
APPOINTMENTS_PER_DAY_FUTURE = (1, 3)
DURATION_SLOTS = (1, 2, 3)  # длительность приёма в слотах и вероятности (максимум по правилам - 4)
DURATION_WEIGHTS = (0.75, 0.2, 0.05)
PAST_STATUSES = (AppointmentStatus.COMPLETED, AppointmentStatus.NO_SHOW, AppointmentStatus.CANCELLED)
PAST_STATUS_WEIGHTS = (0.72, 0.10, 0.18)
# Кто создал запись (created_by): регистратор / администратор / сам пациент. Пациенты с аккаунтом
# часто записываются сами, остальных записывает персонал.
BOOKED_BY_ROLES = ("registrar", "admin", "patient")
BOOKED_BY_WEIGHTS_WITH_ACCOUNT = (0.50, 0.05, 0.45)
BOOKED_BY_WEIGHTS_STAFF_ONLY = (0.88, 0.12, 0.0)
PRESCRIPTION_PROBABILITY = 0.6  # доля завершённых приёмов с диагнозом, по которому выписан рецепт

POLICY_BASE = 7_700_000_000_000_000  # номера полисов: 16 цифр, вычисляются из номера пациента
POLICY_STEP = 1_234_567_891


class SeedError(RuntimeError):
    """Данные в БД несовместимы с демо-набором (например, email занят пользователем другой роли)."""


# --------------------------------------------------------------------------- #
# Исходные данные
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class StaffSpec:
    """Сотрудник без профиля: администратор или регистратор."""

    email: str
    full_name: str
    role: UserRole


@dataclass(frozen=True, slots=True)
class DoctorSpec:
    """Врач и его график (время - местное время клиники, дни - ISO: 1 = понедельник)."""

    email: str
    full_name: str
    specialty: str
    cabinet: str
    slot_minutes: int
    work_start: time
    work_end: time
    work_days: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class PatientSpec:
    """Пациент; ``email`` задан, если у пациента есть учётная запись."""

    full_name: str
    birth_date: date
    email: str | None = None


ADMIN = StaffSpec(f"admin@{EMAIL_DOMAIN}", "Орлов Дмитрий Андреевич", UserRole.ADMIN)
REGISTRAR = StaffSpec(f"registrar@{EMAIL_DOMAIN}", "Петрова Ольга Николаевна", UserRole.REGISTRAR)

# Графики подобраны так, чтобы смена укладывалась в целое число слотов: 16 по 30 мин, 24 по 20 мин,
# 8 по 45 мин, 24 по 15 мин. У врачей разные рабочие дни, в том числе суббота.
DOCTORS: tuple[DoctorSpec, ...] = (
    DoctorSpec(
        email=f"dr.sokolova@{EMAIL_DOMAIN}",
        full_name="Соколова Елена Викторовна",
        specialty="Терапевт",
        cabinet="101",
        slot_minutes=30,
        work_start=time(9),
        work_end=time(17),
        work_days=(1, 2, 3, 4, 5),
    ),
    DoctorSpec(
        email=f"dr.mironov@{EMAIL_DOMAIN}",
        full_name="Миронов Андрей Павлович",
        specialty="Кардиолог",
        cabinet="214",
        slot_minutes=20,
        work_start=time(10),
        work_end=time(18),
        work_days=(1, 2, 4, 5),
    ),
    DoctorSpec(
        email=f"dr.lebedeva@{EMAIL_DOMAIN}",
        full_name="Лебедева Наталья Игоревна",
        specialty="Невролог",
        cabinet="305",
        slot_minutes=45,
        work_start=time(9),
        work_end=time(15),
        work_days=(2, 3, 4, 6),
    ),
    DoctorSpec(
        email=f"dr.kozlov@{EMAIL_DOMAIN}",
        full_name="Козлов Сергей Михайлович",
        specialty="Офтальмолог",
        cabinet="118",
        slot_minutes=15,
        work_start=time(8),
        work_end=time(14),
        work_days=(1, 3, 5, 6),
    ),
)

PATIENTS: tuple[PatientSpec, ...] = (
    PatientSpec("Иванов Павел Сергеевич", date(1985, 3, 14), f"ivanov.pavel@{EMAIL_DOMAIN}"),
    PatientSpec("Смирнова Анна Дмитриевна", date(1992, 7, 21), f"smirnova.anna@{EMAIL_DOMAIN}"),
    PatientSpec("Кузнецов Алексей Николаевич", date(1978, 11, 2), f"kuznetsov.alexey@{EMAIL_DOMAIN}"),
    PatientSpec("Попова Мария Александровна", date(2001, 1, 30)),
    PatientSpec("Васильев Игорь Петрович", date(1969, 5, 17)),
    PatientSpec("Петрова Татьяна Ивановна", date(1957, 9, 8)),
    PatientSpec("Соколов Дмитрий Андреевич", date(1990, 12, 25)),
    PatientSpec("Михайлова Екатерина Сергеевна", date(1996, 4, 12)),
    PatientSpec("Новиков Артём Владимирович", date(2004, 6, 19)),
    PatientSpec("Фёдорова Ольга Борисовна", date(1974, 2, 3)),
    PatientSpec("Морозов Владимир Игоревич", date(1963, 10, 29)),
    PatientSpec("Волкова Наталья Викторовна", date(1988, 8, 16)),
    PatientSpec("Алексеев Никита Олегович", date(1999, 3, 5)),
    PatientSpec("Лебедев Роман Анатольевич", date(1981, 7, 7)),
    PatientSpec("Семёнова Ирина Павловна", date(1950, 12, 11)),
    PatientSpec("Егоров Максим Денисович", date(1994, 9, 23)),
    PatientSpec("Павлова Юлия Андреевна", date(1979, 5, 28)),
    PatientSpec("Козлова Светлана Михайловна", date(1966, 1, 15)),
    PatientSpec("Степанов Георгий Валентинович", date(1955, 4, 9)),
    PatientSpec("Николаева Дарья Евгеньевна", date(2003, 11, 21)),
)


@dataclass(frozen=True, slots=True)
class RxSpec:
    """Вариант назначения: препарат, дозировка, указания."""

    medication: str
    dosage: str
    instructions: str


@dataclass(frozen=True, slots=True)
class DiagnosisSpec:
    """Диагноз по МКБ-10, текст заметки врача и возможные назначения (может не быть)."""

    code: str
    notes: str
    prescriptions: tuple[RxSpec, ...] = ()


@dataclass(frozen=True, slots=True)
class ClinicalProfile:
    """Типичные причины обращения и диагнозы специальности."""

    reasons: tuple[str, ...]
    diagnoses: tuple[DiagnosisSpec, ...]


CLINICAL: dict[str, ClinicalProfile] = {
    "Терапевт": ClinicalProfile(
        reasons=(
            "Плановый осмотр",
            "Кашель и повышенная температура",
            "Повышенное артериальное давление",
            "Боль в пояснице",
            "Справка для работы",
            "Повторный приём",
        ),
        diagnoses=(
            DiagnosisSpec(
                "J06.9",
                "ОРВИ лёгкого течения. Першение в горле, температура до 37,5. Режим, обильное питьё, наблюдение.",
                (RxSpec("Парацетамол", "500 мг", "По 1 таблетке при температуре выше 38, не более 3 раз в сутки"),),
            ),
            DiagnosisSpec(
                "J20.9",
                "Острый бронхит. Кашель с мокротой 4 дня, хрипов нет. Явка на повторный осмотр через неделю.",
                (RxSpec("Амброксол", "30 мг", "По 1 таблетке 3 раза в день после еды, 7 дней"),),
            ),
            DiagnosisSpec(
                "I10",
                "Эссенциальная гипертензия. АД 150/95, жалобы на головную боль. Назначен контроль давления дома.",
                (RxSpec("Лизиноприл", "10 мг", "По 1 таблетке утром, постоянно; измерять давление ежедневно"),),
            ),
            DiagnosisSpec(
                "K29.7",
                "Гастрит неуточнённый. Тяжесть в эпигастрии после еды. Диета, исключить острое и алкоголь.",
                (RxSpec("Омепразол", "20 мг", "По 1 капсуле утром за 30 минут до еды, 14 дней"),),
            ),
            DiagnosisSpec(
                "M54.5",
                "Боль внизу спины. Ограничение наклонов, неврологического дефицита нет. Ограничить нагрузки.",
                (RxSpec("Ибупрофен", "400 мг", "По 1 таблетке 2 раза в день после еды, до 5 дней"),),
            ),
            DiagnosisSpec(
                "E11.9",
                "Сахарный диабет 2 типа без осложнений. Глюкоза натощак 7,2. Направление на анализ HbA1c.",
                (RxSpec("Метформин", "850 мг", "По 1 таблетке 2 раза в день во время еды"),),
            ),
        ),
    ),
    "Кардиолог": ClinicalProfile(
        reasons=(
            "Боли в области сердца",
            "Контроль артериального давления",
            "Одышка при нагрузке",
            "Перебои в работе сердца",
            "Повторный приём",
            "Плановый осмотр",
        ),
        diagnoses=(
            DiagnosisSpec(
                "I10",
                "Гипертоническая болезнь, давление выше целевого. Терапия скорректирована, ЭКГ без острой патологии.",
                (RxSpec("Амлодипин", "5 мг", "По 1 таблетке 1 раз в день утром"),),
            ),
            DiagnosisSpec(
                "I20.8",
                "Стенокардия напряжения: приступы на лестнице, проходят в покое. Назначена нагрузочная проба.",
                (RxSpec("Бисопролол", "5 мг", "По 1 таблетке утром, пульс не ниже 55 в минуту"),),
            ),
            DiagnosisSpec(
                "I25.1",
                "Атеросклеротическая болезнь сердца. Липиды повышены: диета, контроль через 3 месяца.",
                (RxSpec("Аторвастатин", "20 мг", "По 1 таблетке вечером, постоянно"),),
            ),
            DiagnosisSpec(
                "I48.9",
                "Фибрилляция предсердий, пароксизмальная форма. Суточное мониторирование ЭКГ, наблюдение.",
            ),
            DiagnosisSpec(
                "I50.0",
                "Хроническая сердечная недостаточность I-II ФК. Отёков нет, одышка при нагрузке. Контроль веса.",
                (RxSpec("Эналаприл", "5 мг", "По 1 таблетке 2 раза в день"),),
            ),
        ),
    ),
    "Невролог": ClinicalProfile(
        reasons=(
            "Головные боли",
            "Боль в шее",
            "Нарушение сна",
            "Онемение в руке",
            "Повторный приём",
            "Плановый осмотр",
        ),
        diagnoses=(
            DiagnosisSpec(
                "G43.9",
                "Мигрень неуточнённая. Приступы 2-3 раза в месяц с тошнотой. Рекомендован дневник головной боли.",
                (
                    RxSpec(
                        "Суматриптан", "50 мг", "По 1 таблетке в начале приступа; повторно не ранее чем через 2 часа"
                    ),
                ),
            ),
            DiagnosisSpec(
                "G44.2",
                "Головная боль напряжения. Связана с работой за компьютером, очаговой симптоматики нет.",
                (RxSpec("Ибупрофен", "400 мг", "По 1 таблетке при боли, не более 3 раз в сутки"),),
            ),
            DiagnosisSpec(
                "M54.2",
                "Цервикалгия. Болезненность паравертебральных точек, ограничение поворотов. ЛФК, массаж.",
            ),
            DiagnosisSpec(
                "G47.0",
                "Расстройства засыпания. Режим сна, ограничение кофеина вечером; повторная консультация через месяц.",
                (RxSpec("Мелатонин", "3 мг", "По 1 таблетке за 30 минут до сна, 14 дней"),),
            ),
            DiagnosisSpec(
                "G56.0",
                "Синдром запястного канала. Онемение пальцев по ночам. Ортез на ночь, направление на ЭНМГ.",
            ),
        ),
    ),
    "Офтальмолог": ClinicalProfile(
        reasons=(
            "Снижение остроты зрения",
            "Покраснение и зуд глаз",
            "Подбор очков",
            "Сухость в глазах",
            "Профилактический осмотр",
            "Повторный приём",
        ),
        diagnoses=(
            DiagnosisSpec("H52.1", "Миопия слабой степени. Подобрана коррекция, рекомендован осмотр раз в год."),
            DiagnosisSpec(
                "H10.1",
                "Острый атопический конъюнктивит. Отёк и зуд век, выделения скудные. Исключить контакт с аллергеном.",
                (RxSpec("Олопатадин 0,1%", "1 капля", "По 1 капле в каждый глаз 2 раза в день, 10 дней"),),
            ),
            DiagnosisSpec(
                "H04.1",
                "Синдром сухого глаза. Работа за экраном, жжение к вечеру. Режим работы, увлажнение.",
                (RxSpec("Гипромеллоза 0,32%", "1-2 капли", "По 1-2 капли в каждый глаз до 4 раз в день"),),
            ),
            DiagnosisSpec("H25.9", "Старческая катаракта начальная. Острота зрения снижена незначительно, наблюдение."),
            DiagnosisSpec(
                "H40.9",
                "Подозрение на глаукому. Внутриглазное давление на верхней границе нормы, назначено обследование.",
                (RxSpec("Тимолол 0,5%", "1 капля", "По 1 капле в оба глаза утром и вечером"),),
            ),
            DiagnosisSpec("H52.4", "Пресбиопия. Подобраны очки для работы на близком расстоянии."),
        ),
    ),
}


# --------------------------------------------------------------------------- #
# Планирование записей (чистая логика, без БД)
# --------------------------------------------------------------------------- #

Interval = tuple[datetime, datetime]


@dataclass(frozen=True, slots=True)
class PlannedAppointment:
    """Запись, спланированная до обращения к БД: врач и пациент заданы индексами."""

    doctor_idx: int
    patient_idx: int
    starts_at: datetime
    ends_at: datetime
    status: AppointmentStatus
    booked_by: str  # "registrar" | "admin" | "patient"
    created_at: datetime


def schedule_of(doctor: Doctor) -> scheduling.Schedule:
    """График врача из его профиля в БД в виде ``scheduling.Schedule``."""
    return scheduling.Schedule(
        work_days=frozenset(doctor.work_days),
        work_start=doctor.work_start,
        work_end=doctor.work_end,
        slot_minutes=doctor.slot_minutes,
    )


def _is_free(busy: Sequence[Interval], starts_at: datetime, ends_at: datetime) -> bool:
    """Не пересекается ли ``[starts_at, ends_at)`` ни с одним занятым интервалом."""
    return not any(scheduling.intervals_overlap(starts_at, ends_at, b_start, b_end) for b_start, b_end in busy)


def _pick_run(free: Sequence[scheduling.Slot], *, now: datetime, rng: random.Random) -> Interval | None:
    """Случайный отрезок из 1-3 подряд идущих свободных слотов.

    Слот годится, если он целиком в прошлом (закончился не позже ``now``) или начинается позже
    ``now + FUTURE_MARGIN``: приёмов «прямо сейчас» в демо-данных нет. Подряд идущие слоты
    не могут лежать по разные стороны от ``now``: между ними был бы зазор.
    """

    def usable(slot: scheduling.Slot) -> bool:
        return slot.ends_at <= now or slot.starts_at > now + FUTURE_MARGIN

    candidates = [i for i, slot in enumerate(free) if usable(slot)]
    if not candidates:
        return None
    first = rng.choice(candidates)
    wanted = rng.choices(DURATION_SLOTS, weights=DURATION_WEIGHTS)[0]
    last = first
    while (
        last - first + 1 < wanted
        and last + 1 < len(free)
        and free[last + 1].starts_at == free[last].ends_at
        and usable(free[last + 1])
    ):
        last += 1
    return free[first].starts_at, free[last].ends_at


def plan_appointments(
    schedules: Sequence[scheduling.Schedule],
    patient_has_account: Sequence[bool],
    *,
    now: datetime,
    tz: ZoneInfo,
    rng: random.Random,
) -> list[PlannedAppointment]:
    """Планирует записи всех врачей на окно ``[сегодня - 30 дней, сегодня + 14 дней]``.

    Для каждого врача и рабочего дня выбирается несколько свободных отрезков сетки слотов
    (``scheduling.free_slots`` с «нулевой» точкой отсчёта в начале суток возвращает все не занятые
    слоты дня, в том числе прошедшие), подбирается пациент без пересекающихся записей, затем
    назначается статус: прошедшая запись - completed / no_show / cancelled, будущая - scheduled.
    Отменённые записи тоже занимают время: в демо-данных нет пересечений вообще.
    """
    today = now.astimezone(tz).date()
    patient_count = len(patient_has_account)
    doctor_busy: list[list[Interval]] = [[] for _ in schedules]
    patient_busy: list[list[Interval]] = [[] for _ in range(patient_count)]
    planned: list[PlannedAppointment] = []

    for doctor_idx, schedule in enumerate(schedules):
        for offset in range(-HISTORY_DAYS, FUTURE_DAYS + 1):
            day = today + timedelta(days=offset)
            if day.isoweekday() not in schedule.work_days:
                continue
            day_start, _ = scheduling.local_day_bounds(day, tz)
            low, high = APPOINTMENTS_PER_DAY_PAST if offset < 0 else APPOINTMENTS_PER_DAY_FUTURE
            for _ in range(rng.randint(low, high)):
                free = scheduling.free_slots(
                    schedule, day, tz, doctor_busy[doctor_idx], now=day_start - timedelta(seconds=1)
                )
                run = _pick_run(free, now=now, rng=rng)
                if run is None:
                    break
                starts_at, ends_at = run
                patients = [p for p in range(patient_count) if _is_free(patient_busy[p], starts_at, ends_at)]
                if not patients:
                    continue
                patient_idx = rng.choice(patients)

                if starts_at > now:
                    status = AppointmentStatus.SCHEDULED
                    created_at = now - timedelta(minutes=rng.randint(30, 5 * 24 * 60))  # записались недавно
                else:
                    status = rng.choices(PAST_STATUSES, weights=PAST_STATUS_WEIGHTS)[0]
                    created_at = starts_at - timedelta(hours=rng.randint(3, 10 * 24))  # записались заранее
                weights = (
                    BOOKED_BY_WEIGHTS_WITH_ACCOUNT if patient_has_account[patient_idx] else BOOKED_BY_WEIGHTS_STAFF_ONLY
                )
                booked_by = rng.choices(BOOKED_BY_ROLES, weights=weights)[0]

                doctor_busy[doctor_idx].append((starts_at, ends_at))
                patient_busy[patient_idx].append((starts_at, ends_at))
                planned.append(
                    PlannedAppointment(doctor_idx, patient_idx, starts_at, ends_at, status, booked_by, created_at)
                )
    verify_plan(planned, schedules, tz=tz)
    return planned


def verify_plan(
    planned: Sequence[PlannedAppointment], schedules: Sequence[scheduling.Schedule], *, tz: ZoneInfo
) -> None:
    """Независимая проверка плана теми же функциями ``scheduling``, что использует API записи.

    Raises:
        RuntimeError: запись нарушает правило расписания или пересекается с другой записью врача
            либо пациента (это ошибка самого скрипта, а не данных).
    """
    for item in planned:
        try:
            scheduling.validate_booking_window(item.starts_at, now=item.created_at)
            scheduling.validate_within_shift(item.starts_at, item.ends_at, schedules[item.doctor_idx], tz)
            scheduling.validate_slot_grid(item.starts_at, item.ends_at, schedules[item.doctor_idx], tz)
        except scheduling.SchedulingError as exc:
            raise RuntimeError(f"seed plan violates a scheduling rule: {exc}") from exc

    groups: dict[tuple[str, int], list[Interval]] = {}
    for item in planned:
        interval = (item.starts_at, item.ends_at)
        groups.setdefault(("doctor", item.doctor_idx), []).append(interval)
        groups.setdefault(("patient", item.patient_idx), []).append(interval)
    for (kind, owner), intervals in groups.items():
        intervals.sort()
        for (a_start, a_end), (b_start, b_end) in zip(intervals, intervals[1:], strict=False):
            if scheduling.intervals_overlap(a_start, a_end, b_start, b_end):
                raise RuntimeError(f"seed plan has overlapping appointments for {kind} #{owner}")


# --------------------------------------------------------------------------- #
# Запись в БД
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class SeedReport:
    """Итог запуска: что создано и какие логины есть в демо-наборе."""

    created: Counter[str] = field(default_factory=Counter)
    logins: list[tuple[str, str, str]] = field(default_factory=list)  # (роль, email, ФИО)
    history_skipped: bool = False


async def _get_or_create_user(
    session: AsyncSession, email: str, full_name: str, role: UserRole, password: str, report: SeedReport
) -> User:
    """Пользователь по email; если его нет - создаётся с bcrypt-хэшем пароля (существующий не меняется)."""
    user = await session.scalar(select(User).where(User.email == email))
    if user is None:
        user = User(
            email=email,
            password_hash=await hash_password_async(password),
            full_name=full_name,
            role=role,
            is_active=True,
        )
        session.add(user)
        report.created["users"] += 1
    elif user.role != role:
        raise SeedError(f"{email}: пользователь уже существует с ролью {user.role.value!r}, а нужна {role.value!r}")
    report.logins.append((role.value, email, user.full_name))
    return user


async def _ensure_doctors(session: AsyncSession, password: str, report: SeedReport) -> list[Doctor]:
    """Учётные записи и профили демо-врачей (в порядке ``DOCTORS``)."""
    doctors: list[Doctor] = []
    for spec in DOCTORS:
        user = await _get_or_create_user(session, spec.email, spec.full_name, UserRole.DOCTOR, password, report)
        await session.flush()
        doctor = await session.scalar(select(Doctor).where(Doctor.user_id == user.id))
        if doctor is None:
            doctor = Doctor(
                user_id=user.id,
                specialty=spec.specialty,
                cabinet=spec.cabinet,
                slot_minutes=spec.slot_minutes,
                work_start=spec.work_start,
                work_end=spec.work_end,
                work_days=list(spec.work_days),
            )
            session.add(doctor)
            report.created["doctors"] += 1
        doctors.append(doctor)
    await session.flush()
    return doctors


def policy_number(index: int) -> str:
    """Номер полиса ОМС (16 цифр) пациента с порядковым номером ``index`` (с нуля); одинаков при каждом запуске."""
    return f"{POLICY_BASE + (index + 1) * POLICY_STEP:016d}"


def phone_number(index: int) -> str:
    """Вымышленный телефон в формате E.164 для пациента с порядковым номером ``index``."""
    return f"+7900555{index + 1:04d}"


async def _ensure_patients(
    session: AsyncSession, password: str, report: SeedReport
) -> list[tuple[Patient, int | None]]:
    """Пациенты (поиск по номеру полиса) и аккаунты тех, у кого они заданы.

    Returns:
        Пары ``(пациент, id пользователя или None)`` в порядке ``PATIENTS``.
    """
    result: list[tuple[Patient, int | None]] = []
    for index, spec in enumerate(PATIENTS):
        user_id: int | None = None
        if spec.email is not None:
            user = await _get_or_create_user(session, spec.email, spec.full_name, UserRole.PATIENT, password, report)
            await session.flush()
            user_id = user.id
        patient = await session.scalar(select(Patient).where(Patient.policy_number == policy_number(index)))
        if patient is None:
            patient = Patient(
                user_id=user_id,
                full_name=spec.full_name,
                birth_date=spec.birth_date,
                phone=phone_number(index),
                policy_number=policy_number(index),
            )
            session.add(patient)
            report.created["patients"] += 1
        result.append((patient, user_id))
    await session.flush()
    return result


async def _create_history(
    session: AsyncSession,
    doctors: Sequence[Doctor],
    patients: Sequence[tuple[Patient, int | None]],
    staff_ids: dict[str, int],
    *,
    now: datetime,
    rng: random.Random,
    report: SeedReport,
) -> None:
    """Записи на приём, записи медкарты и рецепты. Не вызывается, если у демо-врачей уже есть записи."""
    tz = get_settings().clinic_tz
    planned = plan_appointments(
        [schedule_of(doctor) for doctor in doctors],
        [user_id is not None for _, user_id in patients],
        now=now,
        tz=tz,
        rng=rng,
    )

    appointments: list[Appointment] = []
    for item in planned:
        patient, patient_user_id = patients[item.patient_idx]
        creator = (
            patient_user_id
            if item.booked_by == "patient" and patient_user_id is not None
            else staff_ids[item.booked_by]
        )
        profile = CLINICAL[DOCTORS[item.doctor_idx].specialty]
        appointments.append(
            Appointment(
                doctor_id=doctors[item.doctor_idx].id,
                patient_id=patient.id,
                starts_at=item.starts_at,
                ends_at=item.ends_at,
                status=item.status,
                reason=rng.choice(profile.reasons),
                created_by=creator,
                created_at=item.created_at,
            )
        )
    session.add_all(appointments)
    await session.flush()
    report.created["appointments"] += len(appointments)

    for item, appointment in zip(planned, appointments, strict=True):
        if item.status != AppointmentStatus.COMPLETED:
            continue
        diagnosis = rng.choice(CLINICAL[DOCTORS[item.doctor_idx].specialty].diagnoses)
        recorded_at = min(item.ends_at + timedelta(minutes=rng.randint(5, 30)), now)
        session.add(
            MedicalRecord(
                patient_id=appointment.patient_id,
                doctor_id=appointment.doctor_id,
                appointment_id=appointment.id,
                diagnosis_code=diagnosis.code,
                notes=diagnosis.notes,
                created_at=recorded_at,
            )
        )
        report.created["medical_records"] += 1
        if diagnosis.prescriptions and rng.random() < PRESCRIPTION_PROBABILITY:
            rx = rng.choice(diagnosis.prescriptions)
            session.add(
                Prescription(
                    appointment_id=appointment.id,
                    medication=rx.medication,
                    dosage=rx.dosage,
                    instructions=rx.instructions,
                    issued_at=recorded_at,
                )
            )
            report.created["prescriptions"] += 1


async def seed(session: AsyncSession, password: str, *, now: datetime) -> SeedReport:
    """Создаёт демо-данные в одной транзакции (``commit`` - в вызывающем коде).

    Args:
        session: открытая сессия БД.
        password: пароль всех демо-пользователей.
        now: «текущий момент» (aware): от него отсчитываются 30 дней назад и 14 вперёд.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    report = SeedReport()
    rng = random.Random(RNG_SEED)  # noqa: S311 - детерминированные демо-данные, не криптография

    staff_ids: dict[str, int] = {}
    for key, spec in (("admin", ADMIN), ("registrar", REGISTRAR)):
        user = await _get_or_create_user(session, spec.email, spec.full_name, spec.role, password, report)
        await session.flush()
        staff_ids[key] = user.id
    doctors = await _ensure_doctors(session, password, report)
    patients = await _ensure_patients(session, password, report)

    existing = await session.scalar(
        select(func.count(Appointment.id)).where(Appointment.doctor_id.in_([doctor.id for doctor in doctors]))
    )
    if existing:
        report.history_skipped = True
    else:
        await _create_history(session, doctors, patients, staff_ids, now=now, rng=rng, report=report)
    return report


# --------------------------------------------------------------------------- #
# Командная строка
# --------------------------------------------------------------------------- #


def _aware_datetime(value: str) -> datetime:
    """Разбирает ``--now``: ISO 8601 со смещением, например ``2026-10-12T09:00:00+03:00``."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"не дата ISO 8601: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("укажите смещение часового пояса, например +03:00 или Z")
    return parsed.astimezone(UTC)


def _password_problem(password: str) -> str | None:
    """Почему пароль не годится (те же правила, что у API: длина и 72 байта bcrypt), или ``None``."""
    if not password:
        return "переменная окружения SEED_PASSWORD не задана"
    if len(password) < PASSWORD_MIN_LENGTH:
        return f"SEED_PASSWORD короче {PASSWORD_MIN_LENGTH} символов"
    if len(password.encode("utf-8")) > PASSWORD_MAX_BYTES:
        return f"SEED_PASSWORD длиннее {PASSWORD_MAX_BYTES} байт в UTF-8 (предел bcrypt)"
    return None


def _print_report(report: SeedReport) -> None:
    """Печатает итог и список логинов (пароль не печатается никогда)."""
    if report.created:
        summary = ", ".join(f"{name}: {count}" for name, count in report.created.items())
        print(f"Создано - {summary}.")
    else:
        print("Все демо-данные уже были в базе, ничего не изменено.")
    if report.history_skipped and report.created:
        print("История записей не создавалась: у демо-врачей уже есть записи.")
    print("\nЛогины демо-пользователей (пароль - значение SEED_PASSWORD):")
    width = max(len(email) for _, email, _ in report.logins)
    for role, email, full_name in report.logins:
        print(f"  {role:<10} {email:<{width}}  {full_name}")


async def _run(password: str, now: datetime) -> int:
    """Выполняет посев в одной транзакции и печатает результат; возвращает код завершения."""
    try:
        async with SessionFactory() as session:
            try:
                report = await seed(session, password, now=now)
                await session.commit()
            except SeedError as exc:
                await session.rollback()
                print(f"Ошибка: {exc}", file=sys.stderr)
                return 1
    finally:
        await engine.dispose()
    _print_report(report)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Точка входа ``python -m scripts.seed``; возвращает код завершения процесса."""
    parser = argparse.ArgumentParser(prog="python -m scripts.seed", description="Заполняет БД демо-данными.")
    parser.add_argument(
        "--now",
        type=_aware_datetime,
        default=None,
        help="«текущий момент» в ISO 8601 со смещением (по умолчанию - сейчас); нужен для воспроизводимости",
    )
    args = parser.parse_args(argv)

    password = os.environ.get("SEED_PASSWORD", "")
    problem = _password_problem(password)
    if problem is not None:
        print(f"Ошибка: {problem}. Пример: SEED_PASSWORD='...' python -m scripts.seed", file=sys.stderr)
        return 2
    return asyncio.run(_run(password, args.now or datetime.now(UTC)))


if __name__ == "__main__":
    sys.exit(main())
```

### Dockerfile

```dockerfile
# syntax=docker/dockerfile:1
#
# Образ приложения «Система управления клиникой» (FastAPI + uvicorn).
#
#   docker build -t clinic-app .
#   docker run --rm -p 8000:8000 -e DATABASE_URL=... -e JWT_SECRET=... clinic-app
#
# Полный стенд с PostgreSQL и миграциями: docker compose up -d --build (см. docker-compose.yml).

# --------------------------------------------------------------------------- #
# Этап 1. Виртуальное окружение с зависимостями приложения (без dev-зависимостей)
# --------------------------------------------------------------------------- #
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

# В слой попадает только pyproject.toml, поэтому зависимости пересобираются лишь при его изменении.
# Берётся список [project].dependencies; [project.optional-dependencies].dev (pytest, ruff, ...) не ставится.
# Сам проект через `pip install .` не устанавливается: в pyproject.toml нет [build-system] и настроек
# поиска пакетов, а в корне репозитория несколько каталогов верхнего уровня - автоопределение пакетов
# setuptools на такой раскладке не сработало бы. Код приложения копируется в рантайм-этапе.
COPY pyproject.toml ./
RUN python -c "import tomllib; print(*tomllib.load(open('pyproject.toml', 'rb'))['project']['dependencies'], sep='\n')" \
        > requirements.txt \
    && pip install -r requirements.txt

# --------------------------------------------------------------------------- #
# Этап 2. Рантайм: только окружение и код, без pip-кэша и инструментов сборки
# --------------------------------------------------------------------------- #
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}"

# Непривилегированный пользователь с фиксированным числовым UID (нужно для runAsNonRoot в Kubernetes).
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin app

COPY --from=builder /opt/venv /opt/venv

WORKDIR /srv
COPY --chown=app:app alembic.ini ./
COPY --chown=app:app app ./app
COPY --chown=app:app migrations ./migrations
COPY --chown=app:app scripts ./scripts

USER 10001:10001
EXPOSE 8000

# curl в slim-образе нет, поэтому проверка - короткая программа на Python из окружения образа.
# urlopen бросает исключение при ответе 4xx/5xx и при недоступности порта -> код возврата 1.
HEALTHCHECK --interval=10s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

### .dockerignore

```text
# Всё, что не нужно образу, не попадает в контекст сборки: быстрее сборка и нет утечки секретов.

# Секреты и локальные базы данных
.env
.env.*
*.db
*.sqlite
*.sqlite3

# Система контроля версий, CI, редакторы
.git
.gitignore
.gitattributes
.github
.idea
.vscode

# Python: окружения, кэши, отчёты о покрытии
.venv
venv
__pycache__
*.py[cod]
.pytest_cache
.ruff_cache
.mypy_cache
.coverage
coverage.xml
htmlcov

# Тесты, учебные материалы и документация (в образ не входят)
tests
prompts
reports
docs
*.md

# Файлы самой сборки
Dockerfile
docker-compose*.yml
.dockerignore
```

### docker-compose.yml

```yaml
# Стенд «Система управления клиникой»: PostgreSQL 16 -> одноразовая миграция -> приложение.
#
#   cp .env.example .env                   # PowerShell: Copy-Item .env.example .env
#   # заполните POSTGRES_PASSWORD и JWT_SECRET в .env (команды генерации - в .env.example)
#   docker compose up -d --build           # поднять стенд (миграции применяются автоматически)
#   curl http://127.0.0.1:8000/api/health  # {"status":"ok"}
#   docker compose logs -f app             # журнал приложения
#   docker compose down                    # остановить (данные сохраняются в томе pgdata)
#   docker compose down --volumes          # остановить и УДАЛИТЬ данные
#
# Демо-данные (пароль всех демо-пользователей берётся из переменной окружения оболочки):
#   export SEED_PASSWORD='<пароль не короче 8 символов>'          # PowerShell: $env:SEED_PASSWORD='...'
#   docker compose run --rm --no-deps -e SEED_PASSWORD app python -m scripts.seed
#
# Секретов в этом файле нет: все значения берутся из .env, а обязательные описаны как
# ${VAR:?сообщение} - без них docker compose откажется стартовать с понятной ошибкой.

name: clinic

# Общая часть сервисов migrate и app: один и тот же образ и одинаковые настройки.
# Settings приложения требует и DATABASE_URL, и JWT_SECRET, поэтому JWT_SECRET нужен даже миграции.
x-app: &app
  build:
    context: .
  image: clinic-app:local
  environment:
    DATABASE_URL: "postgresql+asyncpg://${POSTGRES_USER:-clinic}:${POSTGRES_PASSWORD:?задайте POSTGRES_PASSWORD в .env (см. .env.example)}@db:5432/${POSTGRES_DB:-clinic}"
    JWT_SECRET: "${JWT_SECRET:?задайте JWT_SECRET в .env (см. .env.example)}"
    JWT_TTL_MINUTES: "${JWT_TTL_MINUTES:-60}"
    CLINIC_TIMEZONE: "${CLINIC_TIMEZONE:-Europe/Moscow}"
  # Усиление изоляции: приложение ничего не пишет на диск и не требует привилегий.
  read_only: true
  tmpfs:
    - /tmp
  cap_drop:
    - ALL
  security_opt:
    - no-new-privileges:true

services:
  db:
    image: postgres:16
    restart: unless-stopped
    environment:
      POSTGRES_USER: "${POSTGRES_USER:-clinic}"
      POSTGRES_PASSWORD: "${POSTGRES_PASSWORD:?задайте POSTGRES_PASSWORD в .env (см. .env.example)}"
      POSTGRES_DB: "${POSTGRES_DB:-clinic}"
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      # -h 127.0.0.1: во время первичной инициализации PostgreSQL слушает только unix-сокет,
      # и pg_isready без -h сообщил бы «готов» раньше, чем база начнёт принимать TCP-подключения.
      # $$ - экранирование для compose: переменные раскрывает оболочка внутри контейнера.
      test: ["CMD-SHELL", "pg_isready -h 127.0.0.1 -U $${POSTGRES_USER} -d $${POSTGRES_DB}"]
      interval: 5s
      timeout: 5s
      retries: 12
      start_period: 10s

  # Одноразовая миграция схемы; завершается с кодом 0. Повторный запуск безопасен.
  migrate:
    <<: *app
    command: ["alembic", "upgrade", "head"]
    depends_on:
      db:
        condition: service_healthy
    restart: "no"
    healthcheck:
      disable: true # HEALTHCHECK образа проверяет веб-сервер, которого в этом контейнере нет

  app:
    <<: *app
    depends_on:
      db:
        condition: service_healthy
      migrate:
        condition: service_completed_successfully
    restart: unless-stopped
    ports:
      # Только loopback: наружу порт открывайте осознанно (за reverse proxy с TLS).
      - "127.0.0.1:${APP_PORT:-8000}:8000"

volumes:
  pgdata:
```

### .env.example

```bash
# Пример настроек для docker compose. Использование:
#
#   cp .env.example .env              # PowerShell: Copy-Item .env.example .env
#
# затем заполните пустые значения ниже. Файл .env содержит секреты: его нельзя коммитить
# (убедитесь, что `.env` есть в .gitignore), а .env.example хранится в репозитории только как образец.
#
# Секреты оставлены пустыми намеренно: docker compose трактует пустое значение как незаданное
# и не запустится, пока вы не подставите свои. Забытая «заглушка» не станет реальным паролем.

# --- PostgreSQL -------------------------------------------------------------
POSTGRES_USER=clinic
POSTGRES_DB=clinic

# Пароль БД. Он подставляется в DATABASE_URL, поэтому используйте только буквы и цифры (hex).
# Сгенерировать (любой вариант):
#   python -c "import secrets; print(secrets.token_hex(24))"
#   openssl rand -hex 24
POSTGRES_PASSWORD=

# --- Приложение -------------------------------------------------------------
# Ключ подписи JWT (HS256), не короче 32 символов - иначе приложение не запустится.
# Сгенерировать (любой вариант; 64 hex-символа = 256 бит):
#   python -c "import secrets; print(secrets.token_hex(32))"
#   openssl rand -hex 32
# Смена JWT_SECRET делает недействительными все выданные токены.
JWT_SECRET=

# Срок жизни токена в минутах (1..1440) и часовой пояс клиники (название из базы IANA).
JWT_TTL_MINUTES=60
CLINIC_TIMEZONE=Europe/Moscow

# Порт, на котором приложение доступно на хосте (только 127.0.0.1).
APP_PORT=8000

# --- Демо-данные ------------------------------------------------------------
# Пароль демо-пользователей задаётся НЕ здесь, а в окружении оболочки при запуске seed
# (compose его в контейнеры не передаёт):
#   export SEED_PASSWORD='<пароль не короче 8 символов>'
#   docker compose run --rm --no-deps -e SEED_PASSWORD app python -m scripts.seed

# --- Запуск без Docker ------------------------------------------------------
# Если запускаете uvicorn / alembic прямо на хосте, задайте ещё DATABASE_URL (compose его
# собирает сам и эту переменную игнорирует), например:
#   DATABASE_URL=sqlite+aiosqlite:///./clinic.db
#   DATABASE_URL=postgresql+asyncpg://clinic:<пароль>@localhost:5432/clinic
```

### .github/workflows/ci.yml

```yaml
name: CI

on:
  push:
  pull_request:

# Минимальные права GITHUB_TOKEN для всех заданий: только чтение репозитория.
permissions:
  contents: read

# Новый коммит в той же ветке / PR отменяет ещё идущий прогон.
concurrency:
  group: ci-${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true

defaults:
  run:
    shell: bash # bash -eo pipefail: ошибка в любом звене конвейера роняет шаг

env:
  PYTHON_VERSION: "3.12"

# Версии actions зафиксированы точными тегами (обновляйте через Dependabot, для максимальной защиты
# цепочки поставок замените теги на SHA коммитов).
# Зависимости ставятся из pyproject.toml: [project].dependencies + [project.optional-dependencies].dev.
# `pip install .` не используется: в pyproject.toml нет [build-system], а в корне несколько каталогов верхнего уровня.

jobs:
  lint:
    name: Lint (ruff)
    runs-on: ubuntu-24.04
    timeout-minutes: 10
    steps:
      - name: Checkout
        uses: actions/checkout@v4.2.2
        with:
          persist-credentials: false

      - name: Set up Python
        uses: actions/setup-python@v5.6.0
        with:
          python-version: ${{ env.PYTHON_VERSION }}
          cache: pip
          cache-dependency-path: pyproject.toml

      - name: Install dependencies
        run: |
          python - <<'PY' > "$RUNNER_TEMP/requirements.txt"
          import tomllib
          project = tomllib.load(open("pyproject.toml", "rb"))["project"]
          print(*project["dependencies"], *project["optional-dependencies"]["dev"], sep="\n")
          PY
          python -m pip install --upgrade pip
          pip install -r "$RUNNER_TEMP/requirements.txt"

      - name: ruff check
        run: ruff check .

      - name: ruff format --check
        run: ruff format --check .

  tests-sqlite:
    name: Tests (SQLite)
    runs-on: ubuntu-24.04
    timeout-minutes: 20
    steps:
      - name: Checkout
        uses: actions/checkout@v4.2.2
        with:
          persist-credentials: false

      - name: Set up Python
        uses: actions/setup-python@v5.6.0
        with:
          python-version: ${{ env.PYTHON_VERSION }}
          cache: pip
          cache-dependency-path: pyproject.toml

      - name: Install dependencies
        run: |
          python - <<'PY' > "$RUNNER_TEMP/requirements.txt"
          import tomllib
          project = tomllib.load(open("pyproject.toml", "rb"))["project"]
          print(*project["dependencies"], *project["optional-dependencies"]["dev"], sep="\n")
          PY
          python -m pip install --upgrade pip
          pip install -r "$RUNNER_TEMP/requirements.txt"

      # TEST_DATABASE_URL не задан -> tests/conftest.py использует временный файл SQLite.
      - name: pytest with coverage
        run: pytest -ra --cov --cov-report=term-missing --cov-report=xml

      - name: Upload coverage report
        uses: actions/upload-artifact@v4.6.2
        with:
          name: coverage-sqlite
          path: coverage.xml
          if-no-files-found: error
          retention-days: 7

  tests-postgres:
    name: Tests (PostgreSQL 16)
    runs-on: ubuntu-24.04
    timeout-minutes: 20
    services:
      postgres:
        image: postgres:16
        env:
          # Одноразовая БД внутри runner'а: вход без пароля, секретов в репозитории нет.
          POSTGRES_HOST_AUTH_METHOD: trust
          POSTGRES_DB: clinic_test
        ports:
          - 5432:5432
        # -h 127.0.0.1: во время инициализации PostgreSQL слушает только unix-сокет (см. docker-compose.yml).
        options: >-
          --health-cmd "pg_isready -h 127.0.0.1 -U postgres -d clinic_test"
          --health-interval 5s
          --health-timeout 5s
          --health-retries 12
    env:
      TEST_DATABASE_URL: postgresql+asyncpg://postgres@127.0.0.1:5432/clinic_test
    steps:
      - name: Checkout
        uses: actions/checkout@v4.2.2
        with:
          persist-credentials: false

      - name: Set up Python
        uses: actions/setup-python@v5.6.0
        with:
          python-version: ${{ env.PYTHON_VERSION }}
          cache: pip
          cache-dependency-path: pyproject.toml

      - name: Install dependencies
        run: |
          python - <<'PY' > "$RUNNER_TEMP/requirements.txt"
          import tomllib
          project = tomllib.load(open("pyproject.toml", "rb"))["project"]
          print(*project["dependencies"], *project["optional-dependencies"]["dev"], sep="\n")
          PY
          python -m pip install --upgrade pip
          pip install -r "$RUNNER_TEMP/requirements.txt"

      - name: pytest with coverage (PostgreSQL)
        run: pytest -ra --cov --cov-report=term-missing --cov-report=xml

      - name: Upload coverage report
        uses: actions/upload-artifact@v4.6.2
        with:
          name: coverage-postgres
          path: coverage.xml
          if-no-files-found: error
          retention-days: 7

  migrations:
    name: Migrations (PostgreSQL 16)
    runs-on: ubuntu-24.04
    timeout-minutes: 15
    services:
      postgres:
        image: postgres:16
        env:
          POSTGRES_HOST_AUTH_METHOD: trust
          POSTGRES_DB: clinic_migrations
        ports:
          - 5432:5432
        options: >-
          --health-cmd "pg_isready -h 127.0.0.1 -U postgres -d clinic_migrations"
          --health-interval 5s
          --health-timeout 5s
          --health-retries 12
    env:
      DATABASE_URL: postgresql+asyncpg://postgres@127.0.0.1:5432/clinic_migrations
      # Переменные клиента psql (шаги ниже проверяют БД напрямую).
      PGHOST: 127.0.0.1
      PGUSER: postgres
      PGDATABASE: clinic_migrations
    steps:
      - name: Checkout
        uses: actions/checkout@v4.2.2
        with:
          persist-credentials: false

      - name: Set up Python
        uses: actions/setup-python@v5.6.0
        with:
          python-version: ${{ env.PYTHON_VERSION }}
          cache: pip
          cache-dependency-path: pyproject.toml

      - name: Install dependencies
        run: |
          python - <<'PY' > "$RUNNER_TEMP/requirements.txt"
          import tomllib
          project = tomllib.load(open("pyproject.toml", "rb"))["project"]
          print(*project["dependencies"], *project["optional-dependencies"]["dev"], sep="\n")
          PY
          python -m pip install --upgrade pip
          pip install -r "$RUNNER_TEMP/requirements.txt"

      # Settings приложения требуют JWT_SECRET, хотя миграции его не используют.
      # Значение случайное, одноразовое и скрыто в журнале.
      - name: Generate a throwaway JWT_SECRET
        run: |
          secret="$(openssl rand -hex 32)"
          echo "::add-mask::${secret}"
          echo "JWT_SECRET=${secret}" >> "$GITHUB_ENV"

      - name: alembic upgrade head
        run: alembic upgrade head

      - name: alembic check (схема БД == models.py)
        run: alembic check

      # Защита от пересечений существует только в БД (в моделях её нет), поэтому проверяем её напрямую.
      - name: EXCLUDE constraint rejects overlapping appointments
        run: |
          psql -v ON_ERROR_STOP=1 <<'SQL'
          BEGIN;
          DO $$
          DECLARE
              u_id integer;
              d_id integer;
              p_id integer;
          BEGIN
              INSERT INTO users (email, password_hash, full_name, role)
              VALUES ('ci-doctor@example.test', 'x', 'CI Doctor', 'doctor')
              RETURNING id INTO u_id;
              INSERT INTO doctors (user_id, specialty, cabinet, work_start, work_end, work_days)
              VALUES (u_id, 'Therapist', '1', '09:00', '18:00', '[1,2,3,4,5]')
              RETURNING id INTO d_id;
              INSERT INTO patients (full_name, birth_date, policy_number)
              VALUES ('CI Patient', '1990-01-01', '1234567890123456')
              RETURNING id INTO p_id;

              INSERT INTO appointments (doctor_id, patient_id, starts_at, ends_at, created_by)
              VALUES (d_id, p_id, '2030-01-07 09:00+00', '2030-01-07 10:00+00', u_id);

              -- Другое время начала: частичный уникальный индекс этого не видит, EXCLUDE - видит.
              BEGIN
                  INSERT INTO appointments (doctor_id, patient_id, starts_at, ends_at, created_by)
                  VALUES (d_id, p_id, '2030-01-07 09:30+00', '2030-01-07 10:30+00', u_id);
                  RAISE EXCEPTION 'EXCLUDE did not reject an overlapping appointment';
              EXCEPTION WHEN exclusion_violation THEN
                  RAISE NOTICE 'overlapping appointment rejected (SQLSTATE 23P01)';
              END;

              -- Смежный интервал [10:00, 11:00) допустим: интервалы полуоткрытые.
              INSERT INTO appointments (doctor_id, patient_id, starts_at, ends_at, created_by)
              VALUES (d_id, p_id, '2030-01-07 10:00+00', '2030-01-07 11:00+00', u_id);

              -- Отменённая запись время не занимает.
              INSERT INTO appointments (doctor_id, patient_id, starts_at, ends_at, status, created_by)
              VALUES (d_id, p_id, '2030-01-07 09:00+00', '2030-01-07 10:00+00', 'cancelled', u_id);
          END
          $$;
          ROLLBACK;
          SQL

      - name: alembic downgrade base
        run: alembic downgrade base

      - name: Downgrade removed everything (tables and btree_gist)
        run: |
          tables="$(psql -v ON_ERROR_STOP=1 --tuples-only --no-align --command \
            "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public' AND table_name <> 'alembic_version'")"
          extensions="$(psql -v ON_ERROR_STOP=1 --tuples-only --no-align --command \
            "SELECT count(*) FROM pg_extension WHERE extname = 'btree_gist'")"
          echo "leftover tables: ${tables}, btree_gist installed: ${extensions}"
          test "${tables}" = "0"
          test "${extensions}" = "0"

      - name: alembic upgrade head (again)
        run: alembic upgrade head

      - name: alembic check (again)
        run: alembic check

  docker:
    name: Docker image and compose
    runs-on: ubuntu-24.04
    timeout-minutes: 20
    steps:
      - name: Checkout
        uses: actions/checkout@v4.2.2
        with:
          persist-credentials: false

      # Одноразовые секреты только для этого прогона; в журнале скрыты.
      - name: Create .env with throwaway secrets
        run: |
          postgres_password="$(openssl rand -hex 24)"
          jwt_secret="$(openssl rand -hex 32)"
          echo "::add-mask::${postgres_password}"
          echo "::add-mask::${jwt_secret}"
          {
            echo "POSTGRES_PASSWORD=${postgres_password}"
            echo "JWT_SECRET=${jwt_secret}"
          } > .env

      - name: Validate docker-compose.yml
        run: docker compose config --quiet

      - name: Build the image
        run: docker compose build

      # up -d возвращается, когда db здорова, migrate завершилась с кодом 0 и app запущено;
      # если миграция упала, команда завершится с ошибкой.
      - name: Start the stack (db -> migrate -> app)
        run: docker compose up --detach

      - name: Wait until the app container is healthy (Dockerfile HEALTHCHECK)
        run: |
          container="$(docker compose ps --quiet app)"
          status=""
          for _ in $(seq 1 60); do
            status="$(docker inspect --format '{{.State.Health.Status}}' "${container}")"
            if [ "${status}" = "healthy" ]; then break; fi
            sleep 2
          done
          echo "health status: ${status}"
          test "${status}" = "healthy"

      - name: GET /api/health
        run: |
          response="$(curl --fail --silent --show-error http://127.0.0.1:8000/api/health)"
          echo "${response}"
          test "${response}" = '{"status":"ok"}'

      - name: alembic check inside the container
        run: docker compose run --rm --no-deps -T migrate alembic check

      - name: Seed demo data and log in
        run: |
          SEED_PASSWORD="$(openssl rand -hex 12)"
          export SEED_PASSWORD
          echo "::add-mask::${SEED_PASSWORD}"
          docker compose run --rm --no-deps -T -e SEED_PASSWORD app python -m scripts.seed
          token="$(curl --fail --silent --show-error --request POST http://127.0.0.1:8000/api/auth/login \
            --header 'Content-Type: application/json' \
            --data "{\"email\": \"admin@clinic.example\", \"password\": \"${SEED_PASSWORD}\"}" \
            | python3 -c 'import json, sys; print(json.load(sys.stdin)["access_token"])')"
          echo "::add-mask::${token}"
          curl --fail --silent --show-error --header "Authorization: Bearer ${token}" http://127.0.0.1:8000/api/doctors \
            | python3 -c 'import json, sys; doctors = json.load(sys.stdin); assert len(doctors) == 4, doctors'

      - name: Show container logs
        if: failure()
        run: docker compose logs --no-color

      - name: Tear down
        if: always()
        run: docker compose down --volumes --remove-orphans
```

## Допущения

- **Стек.** Python 3.12, Alembic ≥ 1.13, SQLAlchemy 2.x, PostgreSQL 16, Docker Compose v2 (`docker compose`). Локально проверял на Python 3.12.10, Alembic 1.20.0, SQLAlchemy 2.1.4, asyncpg 0.32.0, ruff 0.17.0.
- **Миграция написана вручную**, а не автогенерацией. Автогенерация Alembic для `Enum(native_enum=False, create_constraint=True)` выдаёт дублирующий CHECK, а для `UTCDateTime` — ссылку на `app.models.UTCDateTime` без импорта. В миграции типы записаны как `sa.String(16)` + CHECK и `sa.DateTime(timezone=True)`, импортов из `app.*` нет, имена ограничений заданы явно через `op.f`.
- **EXCLUDE** называется `ex_appointments_doctor_no_overlap` (в `NAMING_CONVENTION` префикса `ex` нет). Имя продублировано в `migrations/env.py` (`DB_ONLY_OBJECT_NAMES`): `include_object` прячет это ограничение и его GiST-индекс от `alembic check`, поскольку в моделях их нет. Семантика `[)` и `status <> 'cancelled'` совпадает с `scheduling.intervals_overlap` и `_has_overlap`. EXCLUDE охватывает только врача; пересечения у пациента по-прежнему проверяет код.
- **`compare_server_default` не включён** (в задании требовался только `compare_type=True`; сравнение значений по умолчанию Alembic делает приблизительно и может давать ложные срабатывания, а проверить это на PostgreSQL я не мог). Совпадение `server_default` с моделями я проверил сравнением DDL, но `alembic check` их не сверяет.
- **`JWT_SECRET` нужен даже миграциям** — `Settings` валидирует `DATABASE_URL` и `JWT_SECRET` одновременно, поэтому он передаётся и сервису `migrate`, и в CI-задание `migrations`.
- **`pip install .` не используется:** в `pyproject.toml` нет `[build-system]`, а в корне несколько каталогов верхнего уровня (автоопределение пакетов setuptools на такой раскладке не сработало бы). Зависимости берутся через `tomllib` из `[project].dependencies` (в образ) и вместе с `[project.optional-dependencies].dev` (в CI). Lock-файла нет, версии плавающие (`>=`).
- **`.env.example`:** секреты намеренно пустые — `${VAR:?}` в compose считает пустое значение незаданным, так что забытая «заглушка» не превратится в слабый пароль. Пароль БД подставляется в URL, поэтому в нём только буквы и цифры (hex).
- **Seed.** Запускается после миграций. Пароль — `SEED_PASSWORD` (те же правила, что у API: ≥ 8 символов, ≤ 72 байт). Логины в домене `clinic.example` (зарезервированный TLD); у троих пациентов есть аккаунты. История записей создаётся только если у демо-врачей нет ни одной записи; «сегодня» можно зафиксировать флагом `--now`. Все ФИО, полисы и телефоны (`+7900555xxxx`) вымышленные, назначения лекарств — иллюстрация.
- **CI.** Сервисные контейнеры PostgreSQL работают с `POSTGRES_HOST_AUTH_METHOD=trust` (пароля нет, секретов в репозитории нет); для docker-задания секреты одноразовые, генерируются `openssl rand` и маскируются. Actions закреплены точными тегами (`@v4.2.2`, `@v5.6.0`, `@v4.6.2`), а не SHA: SHA коммитов получить офлайн нельзя.
- **Тесты на PostgreSQL** строят схему через `Base.metadata.create_all` (как в `conftest.py`), поэтому EXCLUDE там нет. Саму защиту от пересечений проверяет шаг CI в задании `migrations` (SQL напрямую) плюс юнит-тест классификации SQLSTATE.
- **Что я реально запускал:** `alembic upgrade head → check → downgrade base → upgrade head → check` на SQLite; негативные проверки `alembic check` (лишний столбец и индекс дают код ≠ 0, а «чужой» индекс EXCLUDE игнорируется); сравнение DDL миграции с `CREATE TABLE` из метаданных — для SQLite по `sqlite_master`, для PostgreSQL по `alembic upgrade head --sql` (отличий нет); `ruff check` и `ruff format --check` по новым файлам и `app/`; новые тесты (7 passed); seed дважды на SQLite (идемпотентность, два чистых запуска дают одинаковый набор, 0 пересечений, все записи проходят `validate_*`, вход под демо-пользователями через API). **Не запускалось:** PostgreSQL, Docker/Compose, GitHub Actions (в моей среде их нет); YAML разобран парсером.

## На что обратить внимание при ревью

1. **`alembic check` на PostgreSQL я не гонял** — это главный риск. Возможные расхождения: сравнение типов для `TypeDecorator` (`UTCDateTime`), отражение частичного уникального индекса (предикат `WHERE`), отражение выражения в GiST-индексе EXCLUDE (страхует `include_object`). Первый прогон задания `migrations` — реальная проверка.
2. **Смоук-тест EXCLUDE в CI** (PL/pgSQL в шаге `EXCLUDE constraint rejects overlapping appointments`) написан без прогона: проверьте синтаксис и список обязательных столбцов `users`, `doctors`, `patients`, `appointments`. Ему нужен `psql` на runner'е (по описанию образа `ubuntu-24.04` клиент PostgreSQL установлен; если нет — добавьте шаг `sudo apt-get install -y postgresql-client`).
3. **Расширенный смысл `is_unique_violation`** (`app/errors.py`): функция теперь отвечает и за 23P01. Три вызова (`_commit`, `commit_or_conflict`, `auth.register`) получили это автоматически; 23P01 возможен только на `appointments`. Проверьте, что существующие тесты не опираются на то, что 23P01 «не конфликт».
4. **EXCLUDE и `downgrade`:** `ALTER TABLE … ADD CONSTRAINT … EXCLUDE` берёт `ACCESS EXCLUSIVE` и упадёт, если в таблице уже есть пересекающиеся записи (для пустой начальной схемы это не проблема, но при применении к БД с данными проверьте их заранее). `DROP EXTENSION btree_gist` в `downgrade` без `CASCADE`: упадёт, если от расширения зависит что-то ещё, и удалит расширение, даже если оно было создано до миграции. Для `CREATE EXTENSION` нужно право `CREATE` на базу (в PostgreSQL 13+ `btree_gist` — доверенное расширение).
5. **Seed на PostgreSQL не запускался** — впервые он выполняется в задании `docker` (шаг `Seed demo data and log in`). Проверьте также, что даты и «текущий момент» (UTC против `Europe/Moscow`) выглядят разумно на показе: набор привязан к дню запуска.
6. **Compose:** `read_only: true`, `cap_drop: [ALL]`, `no-new-privileges` — если приложение начнёт писать на диск (загрузки, логи в файл), понадобится том или `tmpfs`. Порт приложения опубликован только на `127.0.0.1`. Образ `clinic-app:local` собирается дважды (для `migrate` и `app`), второй раз из кэша. В `up -d` намеренно нет `--wait`: завершающийся контейнер `migrate` в части версий Compose воспринимается как ошибка — вместо этого CI сам ждёт статус `healthy` у `app`.
7. **Dockerfile:** `HEALTHCHECK` жёстко привязан к `127.0.0.1:8000`; `python:3.12-slim` — плавающий тег (для воспроизводимости зафиксируйте digest); `python -c` в `RUN` рассчитан на `sh` внутри образа.
8. **Файл `.gitignore`** мне недоступен: убедитесь, что в нём есть `.env` (и `*.db`). `.env.example` в репозитории нужен, `.env` — никогда.
9. **Ruff по всему репозиторию.** Я линтил новые файлы и копию `app/`; `ruff check .` и `ruff format --check .` в CI пройдут по остальным тестам и каталогам, которые я не читал.
10. **`alembic.ini` должен оставаться ASCII-only:** Alembic читает его в локали ОС, и русская «И» (байт `0x98` в UTF-8) роняет `alembic` на Windows с cp1251 — это я воспроизвёл.

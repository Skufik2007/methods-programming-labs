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

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


def _unicode_lower(value: str | None) -> str | None:
    """Unicode-версия SQL-функции ``lower`` (встроенная в SQLite понимает только ASCII)."""
    return None if value is None else value.lower()


def create_engine(database_url: str) -> AsyncEngine:
    """Создаёт async engine.

    Для SQLite (тесты) на каждом соединении:

    * включается ``PRAGMA foreign_keys=ON``: по умолчанию SQLite не проверяет внешние
      ключи, и тесты не ловили бы нарушения ссылочной целостности;
    * ``lower()`` заменяется Unicode-версией: иначе поиск без учёта регистра
      (``app.services.dbutil.ci_contains``) не находил бы «Иванов» по запросу «иванов».
    """
    engine = create_async_engine(database_url, pool_pre_ping=True)

    if make_url(database_url).get_backend_name() == "sqlite":

        @event.listens_for(engine.sync_engine, "connect")
        def _configure_sqlite_connection(dbapi_connection: Any, _connection_record: Any) -> None:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
            finally:
                cursor.close()
            dbapi_connection.create_function("lower", 1, _unicode_lower, deterministic=True)

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

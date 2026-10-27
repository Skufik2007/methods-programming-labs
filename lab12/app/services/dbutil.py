"""Общие помощники слоя БД: коммит с превращением гонки в 409 и поиск без учёта регистра.

``ci_contains`` / ``ci_equals`` сравнивают через ``lower()``. В PostgreSQL ``lower()`` корректно
работает с кириллицей, если база в UTF-8 и локаль не ``C``. Встроенный ``lower()`` SQLite
понимает только ASCII, поэтому для SQLite его Unicode-версию регистрирует сам engine
(``app.db.create_engine``) на каждом соединении.
"""

from sqlalchemy import ColumnElement, String, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from app.errors import ConflictError, is_unique_violation


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

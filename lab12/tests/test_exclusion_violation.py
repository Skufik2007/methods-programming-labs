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

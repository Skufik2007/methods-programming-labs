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

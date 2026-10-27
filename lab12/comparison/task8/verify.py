"""Проверка утверждений модели запуском в окружении проекта (задание 8)."""

import importlib
import sys

import fastapi
import jwt
import pydantic
import sqlalchemy
from sqlalchemy.ext.asyncio import AsyncSession

print(
    f"Python {sys.version.split()[0]}, SQLAlchemy {sqlalchemy.__version__}, FastAPI {fastapi.__version__}, "
    f"Pydantic {pydantic.VERSION}, PyJWT {jwt.__version__}"
)


def check(claim: str, probe) -> None:
    try:
        probe()
        print(f"[есть]  {claim}")
    except Exception as exc:  # noqa: BLE001 - фиксируем любой отказ
        print(f"[нет]   {claim}: {type(exc).__name__}: {exc}")


check("AsyncSession.upsert", lambda: AsyncSession.upsert)
check("модуль sqlalchemy.ext.upsert", lambda: importlib.import_module("sqlalchemy.ext.upsert"))
check("модуль fastapi.ratelimit", lambda: importlib.import_module("fastapi.ratelimit"))
check("pydantic.types.SNILS", lambda: pydantic.types.SNILS)
check("jwt.revoke_token", lambda: jwt.revoke_token)
# Что есть на самом деле — для исправления:
check(
    "sqlalchemy.dialects.postgresql.insert(...).on_conflict_do_update",
    lambda: importlib.import_module("sqlalchemy.dialects.postgresql").insert.__call__,
)

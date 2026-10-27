"""Задание 8: исправление галлюцинации «AsyncSession.upsert / sqlalchemy.ext.upsert».

Задача из ответа модели: вставить запись или обновить её, если такая уже есть. В
проекте естественный ключ — номер полиса пациента (уникальный индекс), например при
импорте пациентов из внешней регистратуры.

В SQLAlchemy 2.x нет ни AsyncSession.upsert(), ни модуля sqlalchemy.ext.upsert
(проверено в verify.py). Upsert — это возможность конкретной СУБД, и в SQLAlchemy он
выражается диалектной конструкцией INSERT ... ON CONFLICT:
sqlalchemy.dialects.postgresql.insert(...).on_conflict_do_update(...) (для SQLite —
аналогичная sqlalchemy.dialects.sqlite.insert).

Запуск на PostgreSQL (схема создаётся во временной БД):
    DATABASE_URL=postgresql+asyncpg://... JWT_SECRET=... python comparison/task8/fixed_upsert.py
"""

import asyncio
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app import db, models


async def upsert_patient(session: AsyncSession, *, policy_number: str, full_name: str, phone: str | None) -> int:
    """Вставляет пациента или обновляет ФИО и телефон существующего (по полису). Возвращает id.

    Одна команда INSERT ... ON CONFLICT (policy_number) DO UPDATE: атомарно и без гонки
    «SELECT, затем INSERT», которую дал бы наивный вариант. Дата рождения при обновлении
    не меняется (excluded — значения из отвергнутой вставки).
    """
    stmt = insert(models.Patient).values(
        policy_number=policy_number, full_name=full_name, phone=phone, birth_date=date(1990, 1, 1)
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[models.Patient.policy_number],
        set_={"full_name": stmt.excluded.full_name, "phone": stmt.excluded.phone},
    ).returning(models.Patient.id)
    patient_id = (await session.execute(stmt)).scalar_one()
    await session.commit()
    return patient_id


async def main() -> None:
    async with db.engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.drop_all)
        await conn.run_sync(models.Base.metadata.create_all)
    async with db.SessionFactory() as session:
        first = await upsert_patient(session, policy_number="7" * 16, full_name="Иванов И.", phone=None)
        second = await upsert_patient(session, policy_number="7" * 16, full_name="Иванов Иван", phone="+79990001122")
        rows = await session.scalar(select(func.count()).select_from(models.Patient))
        patient = await session.get(models.Patient, first)
    print(f"id первой вставки = {first}, id повторной = {second}, строк в таблице = {rows}")
    print(f"после повторного вызова: {patient.full_name!r}, {patient.phone!r}")
    assert first == second and rows == 1 and patient.full_name == "Иванов Иван"
    await db.engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())

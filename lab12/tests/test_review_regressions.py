"""Регрессионные тесты на проблемы, найденные при ревью кода ИИ (см. reports/code-review.md).

Номер в имени теста совпадает с номером находки в отчёте.
"""

import asyncio
import os
import subprocess
import sys
import textwrap
from datetime import date, datetime, timedelta

import httpx
from sqlalchemy import func, select

from app import db
from app.main import app
from app.models import Appointment, AppointmentStatus, Patient, User, UserRole
from app.routers.appointments import get_now


async def _patient(session, policy_digit: str) -> Patient:
    patient = Patient(full_name="Пациент", birth_date=date(1990, 1, 1), policy_number=policy_digit * 16)
    session.add(patient)
    await session.commit()
    return patient


async def test_02_extreme_datetime_is_422_not_500(client: httpx.AsyncClient, session, make_user, make_doctor, auth):
    registrar = await make_user(UserRole.REGISTRAR)
    doctor = await make_doctor()
    patient = await _patient(session, "1")

    response = await client.post(
        "/api/appointments",
        json={
            "doctor_id": doctor.id,
            "patient_id": patient.id,
            "starts_at": "0001-01-01T00:00:00+05:00",
            "ends_at": "0001-01-01T01:00:00+05:00",
        },
        headers=auth(registrar),
    )

    assert response.status_code == 422
    assert "out of the supported range" in response.text


async def test_03_started_appointment_cannot_be_cancelled(
    client: httpx.AsyncClient, session, make_user, make_doctor, auth, now: datetime
):
    registrar = await make_user(UserRole.REGISTRAR)
    doctor = await make_doctor()
    doctor_user = await session.get(User, doctor.user_id)
    patient = await _patient(session, "2")
    starts_at = now + timedelta(hours=1)  # 10:00 по Москве — начало слота
    created = await client.post(
        "/api/appointments",
        json={
            "doctor_id": doctor.id,
            "patient_id": patient.id,
            "starts_at": starts_at.isoformat(),
            "ends_at": (starts_at + timedelta(minutes=30)).isoformat(),
        },
        headers=auth(registrar),
    )
    assert created.status_code == 201, created.text
    appointment_id = created.json()["id"]

    app.dependency_overrides[get_now] = lambda: starts_at + timedelta(days=1)  # приём был вчера
    cancel = await client.post(f"/api/appointments/{appointment_id}/cancel", headers=auth(registrar))
    no_show = await client.post(f"/api/appointments/{appointment_id}/no-show", headers=auth(doctor_user))

    assert cancel.status_code == 422
    assert cancel.json()["detail"].startswith("The appointment has already started")
    assert no_show.status_code == 200
    assert no_show.json()["status"] == "no_show"


async def test_04_concurrent_overlapping_bookings_one_wins(
    client: httpx.AsyncClient, session, make_user, make_doctor, auth, now: datetime
):
    registrar = await make_user(UserRole.REGISTRAR)
    doctor = await make_doctor()
    first, second = await _patient(session, "3"), await _patient(session, "4")
    start = now + timedelta(hours=1)

    def body(patient: Patient, starts_at: datetime) -> dict[str, object]:
        return {
            "doctor_id": doctor.id,
            "patient_id": patient.id,
            "starts_at": starts_at.isoformat(),
            "ends_at": (start + timedelta(hours=1)).isoformat(),
        }

    # Интервалы 10:00-11:00 и 10:30-11:00 пересекаются, но начинаются в разное время:
    # частичный уникальный индекс (doctor_id, starts_at) такую пару не ловит.
    responses = await asyncio.gather(
        client.post("/api/appointments", json=body(first, start), headers=auth(registrar)),
        client.post("/api/appointments", json=body(second, start + timedelta(minutes=30)), headers=auth(registrar)),
    )

    assert sorted(r.status_code for r in responses) == [201, 409]
    async with db.SessionFactory() as fresh:
        active = await fresh.scalar(
            select(func.count()).select_from(Appointment).where(Appointment.status != AppointmentStatus.CANCELLED)
        )
    assert active == 1


def test_05_sqlite_unicode_lower_does_not_depend_on_imports():
    # Отдельный процесс импортирует только app.db — как скрипт заполнения БД или миграция.
    # Раньше Unicode-lower регистрировался глобальным обработчиком в app.services.dbutil,
    # и без этого импорта SQL lower('ИВАНОВ') возвращал 'ИВАНОВ'.
    code = textwrap.dedent(
        """
        import asyncio
        from sqlalchemy import text
        from app import db

        async def main():
            async with db.engine.connect() as conn:
                print((await conn.execute(text("select lower('ИВАНОВ')"))).scalar())

        asyncio.run(main())
        """
    )
    env = {**os.environ, "DATABASE_URL": "sqlite+aiosqlite:///:memory:", "PYTHONIOENCODING": "utf-8"}
    result = subprocess.run(  # noqa: S603 - фиксированная команда, без пользовательского ввода
        [sys.executable, "-c", code], capture_output=True, encoding="utf-8", env=env, check=True, timeout=60
    )
    assert result.stdout.strip() == "иванов"


async def test_06_password_change_revokes_issued_tokens(client: httpx.AsyncClient, make_user, auth):
    admin = await make_user(UserRole.ADMIN)
    patient = await make_user(UserRole.PATIENT, email="victim@clinic.test")
    login = await client.post("/api/auth/login", json={"email": "victim@clinic.test", "password": "Password123"})
    stolen = {"Authorization": f"Bearer {login.json()['access_token']}"}
    assert (await client.get("/api/auth/me", headers=stolen)).status_code == 200

    changed = await client.patch(f"/api/users/{patient.id}", json={"password": "NewPassword456"}, headers=auth(admin))
    assert changed.status_code == 200

    assert (await client.get("/api/auth/me", headers=stolen)).status_code == 401
    relogin = await client.post("/api/auth/login", json={"email": "victim@clinic.test", "password": "NewPassword456"})
    fresh = {"Authorization": f"Bearer {relogin.json()['access_token']}"}
    assert (await client.get("/api/auth/me", headers=fresh)).status_code == 200


async def test_07_doctor_lists_only_own_patients_in_one_request(
    client: httpx.AsyncClient, session, make_user, make_doctor, auth, now: datetime
):
    registrar = await make_user(UserRole.REGISTRAR)
    doctor = await make_doctor()
    doctor_user = await session.get(User, doctor.user_id)
    own, foreign = await _patient(session, "5"), await _patient(session, "6")
    starts_at = now + timedelta(hours=1)
    booked = await client.post(
        "/api/appointments",
        json={
            "doctor_id": doctor.id,
            "patient_id": own.id,
            "starts_at": starts_at.isoformat(),
            "ends_at": (starts_at + timedelta(minutes=30)).isoformat(),
        },
        headers=auth(registrar),
    )
    assert booked.status_code == 201, booked.text
    patient_user = await make_user(UserRole.PATIENT)

    listed = await client.get("/api/patients", headers=auth(doctor_user))
    forbidden = await client.get("/api/patients", headers=auth(patient_user))

    assert listed.status_code == 200
    assert [p["id"] for p in listed.json()] == [own.id]
    assert foreign.id not in {p["id"] for p in listed.json()}
    assert forbidden.status_code == 403

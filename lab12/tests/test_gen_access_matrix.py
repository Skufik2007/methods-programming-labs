"""Матрица доступа: эндпоинт x роль (без токена, admin, registrar, doctor, patient)."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.models import Appointment, AppointmentStatus, Patient, User, UserRole

ROLES = ["none", "admin", "registrar", "doctor", "patient"]


@dataclass(frozen=True)
class Case:
    method: str
    path: Callable[[SimpleNamespace], str]
    body: Callable[[SimpleNamespace], dict | None]
    expected: tuple[int, int, int, int, int]  # none, admin, registrar, doctor, patient
    label: str


def _case(method, path, body, expected, label=None):
    return Case(method, path if callable(path) else (lambda w, p=path: p), body, expected, label or f"{method} {path}")


CASES = [
    _case("GET", "/api/auth/me", lambda w: None, (401, 200, 200, 200, 200)),
    _case("GET", "/api/users", lambda w: None, (401, 200, 403, 403, 403)),
    _case(
        "POST",
        "/api/users",
        lambda w: {
            "email": "n@clinic.test",
            "password": "Password123",
            "full_name": "Новый Человек",
            "role": "registrar",
        },
        (401, 201, 403, 403, 403),
    ),
    _case(
        "PATCH",
        lambda w: f"/api/users/{w.spare_user.id}",
        lambda w: {"full_name": "Другое Имя"},
        (401, 200, 403, 403, 403),
        "PATCH /api/users/{existing}",
    ),
    _case("PATCH", "/api/users/999999", lambda w: {"full_name": "Другое Имя"}, (401, 404, 403, 403, 403)),
    _case("GET", "/api/doctors", lambda w: None, (401, 200, 200, 200, 200)),
    _case(
        "GET",
        lambda w: f"/api/doctors/{w.doctor.id}",
        lambda w: None,
        (401, 200, 200, 200, 200),
        "GET /api/doctors/{existing}",
    ),
    _case("GET", "/api/doctors/999999", lambda w: None, (401, 404, 404, 404, 404)),
    _case(
        "POST",
        "/api/doctors",
        lambda w: {
            "user_id": w.new_doctor_user.id,
            "specialty": "Лор",
            "cabinet": "3",
            "work_start": "09:00:00",
            "work_end": "17:00:00",
        },
        (401, 201, 403, 403, 403),
    ),
    _case(
        "PATCH",
        lambda w: f"/api/doctors/{w.doctor.id}",
        lambda w: {"cabinet": "202"},
        (401, 200, 403, 403, 403),
        "PATCH /api/doctors/{existing}",
    ),
    _case("PATCH", "/api/doctors/999999", lambda w: {"cabinet": "202"}, (401, 404, 403, 403, 403)),
    _case("GET", "/api/patients", lambda w: None, (401, 200, 200, 200, 403)),
    _case("GET", "/api/patients/me", lambda w: None, (401, 403, 403, 403, 200)),
    _case(
        "GET",
        lambda w: f"/api/patients/{w.own_patient.id}",
        lambda w: None,
        (401, 200, 200, 200, 200),
        "GET /api/patients/{own-or-visible}",
    ),
    _case(
        "GET",
        lambda w: f"/api/patients/{w.foreign_patient.id}",
        lambda w: None,
        (401, 200, 200, 404, 404),
        "GET /api/patients/{foreign}",
    ),
    _case("GET", "/api/patients/999999", lambda w: None, (401, 404, 404, 404, 404)),
    _case(
        "POST",
        "/api/patients",
        lambda w: {"full_name": "Новый Пациент", "birth_date": "1990-01-01", "policy_number": "9000000000000001"},
        (401, 201, 201, 403, 403),
    ),
    _case(
        "PATCH",
        lambda w: f"/api/patients/{w.own_patient.id}",
        lambda w: {"phone": "+79990000000"},
        (401, 200, 200, 403, 403),
        "PATCH /api/patients/{existing}",
    ),
    _case("PATCH", "/api/patients/999999", lambda w: {"phone": "+79990000000"}, (401, 404, 404, 403, 403)),
]


@pytest.fixture
async def world(session, make_user, make_doctor):
    admin = await make_user(UserRole.ADMIN)
    registrar = await make_user(UserRole.REGISTRAR)
    doctor = await make_doctor()
    doctor_user = await session.get(User, doctor.user_id)
    patient_user = await make_user(UserRole.PATIENT)
    own = Patient(
        user_id=patient_user.id, full_name="Свой Пациент", birth_date=date(1990, 1, 1), policy_number="1000000000000001"
    )
    foreign = Patient(full_name="Чужой Пациент", birth_date=date(1991, 2, 2), policy_number="1000000000000002")
    session.add_all([own, foreign])
    await session.commit()
    start = datetime(2026, 10, 13, 7, 0, tzinfo=UTC)
    session.add(
        Appointment(
            doctor_id=doctor.id,
            patient_id=own.id,
            starts_at=start,
            ends_at=start + timedelta(minutes=30),
            status=AppointmentStatus.SCHEDULED,
            created_by=admin.id,
        )
    )
    await session.commit()
    return SimpleNamespace(
        users={"admin": admin, "registrar": registrar, "doctor": doctor_user, "patient": patient_user},
        doctor=doctor,
        own_patient=own,
        foreign_patient=foreign,
        spare_user=await make_user(UserRole.REGISTRAR),
        new_doctor_user=await make_user(UserRole.DOCTOR),
    )


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.label)
@pytest.mark.parametrize("role", ROLES)
async def test_access_matrix_returns_expected_status(client, auth, world, case, role):
    headers = {} if role == "none" else auth(world.users[role])
    expected = case.expected[ROLES.index(role)]

    response = await client.request(case.method, case.path(world), json=case.body(world), headers=headers)

    assert response.status_code == expected, response.text
    payload = response.json()
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if expected == 401:
        assert detail == "Not authenticated"
        assert response.headers["www-authenticate"] == "Bearer"
    elif expected == 403:
        assert isinstance(detail, str) and detail.startswith("Insufficient permissions")
    elif expected == 404:
        assert isinstance(detail, str) and detail.endswith("not found")

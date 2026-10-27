"""HTTP-тесты назначений: POST /api/prescriptions и GET /api/appointments/{id}/prescriptions."""

import itertools
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app import models
from app.models import AppointmentStatus, UserRole

_seq = itertools.count(1)


async def _user_of(session, doctor):
    return await session.get(models.User, doctor.user_id)


async def _make_patient(session, user=None):
    patient = models.Patient(
        user_id=user.id if user else None,
        full_name="Пациент Тестов",
        birth_date=date(1990, 1, 1),
        policy_number=f"{next(_seq):016d}",
    )
    session.add(patient)
    await session.commit()
    return patient


async def _make_appointment(session, doctor, patient, creator, status=AppointmentStatus.COMPLETED):
    start = datetime(2026, 10, 5, 6, 0, tzinfo=UTC) + timedelta(hours=next(_seq))
    appt = models.Appointment(
        doctor_id=doctor.id,
        patient_id=patient.id,
        starts_at=start,
        ends_at=start + timedelta(minutes=30),
        status=status,
        created_by=creator.id,
    )
    session.add(appt)
    await session.commit()
    return appt


async def _make_prescription(session, appt, issued_at, medication="Аспирин", dosage="1 таб.", instructions=None):
    p = models.Prescription(
        appointment_id=appt.id, medication=medication, dosage=dosage, instructions=instructions, issued_at=issued_at
    )
    session.add(p)
    await session.commit()
    return p


async def _count(session):
    return await session.scalar(select(func.count()).select_from(models.Prescription))


@dataclass
class World:
    doctor: models.Doctor
    doctor_user: models.User
    patient: models.Patient
    patient_user: models.User
    appt: models.Appointment
    registrar: models.User
    admin: models.User


@pytest.fixture
async def world(session, make_user, make_doctor):
    doctor = await make_doctor()
    doctor_user = await _user_of(session, doctor)
    patient_user = await make_user(UserRole.PATIENT)
    patient = await _make_patient(session, patient_user)
    registrar = await make_user(UserRole.REGISTRAR)
    admin = await make_user(UserRole.ADMIN)
    appt = await _make_appointment(session, doctor, patient, registrar)
    return World(doctor, doctor_user, patient, patient_user, appt, registrar, admin)


def _body(w, **over):
    data = {"appointment_id": w.appt.id, "medication": "Амоксициллин", "dosage": "500 мг 3 раза в день"}
    data.update(over)
    return data


# --------------------------------------------------------------------------- #
# Матрица доступа
# --------------------------------------------------------------------------- #


class TestAccessMatrix:
    async def test_create_without_token_returns_401(self, client, world, session):
        r = await client.post("/api/prescriptions", json=_body(world))
        assert r.status_code == 401
        assert r.json() == {"detail": "Not authenticated"}
        assert await _count(session) == 0

    async def test_list_without_token_returns_401(self, client, world):
        r = await client.get(f"/api/appointments/{world.appt.id}/prescriptions")
        assert r.status_code == 401

    @pytest.mark.parametrize("role", ["admin", "registrar", "patient"])
    async def test_create_non_doctor_returns_403(self, client, world, auth, session, role):
        user = {"admin": world.admin, "registrar": world.registrar, "patient": world.patient_user}[role]
        r = await client.post("/api/prescriptions", json=_body(world), headers=auth(user))
        assert r.status_code == 403
        assert r.json() == {"detail": "Insufficient permissions"}
        assert await _count(session) == 0

    @pytest.mark.parametrize("role", ["admin", "registrar"])
    async def test_list_staff_returns_403(self, client, world, auth, role):
        user = world.admin if role == "admin" else world.registrar
        r = await client.get(f"/api/appointments/{world.appt.id}/prescriptions", headers=auth(user))
        assert r.status_code == 403
        assert r.json() == {"detail": "Insufficient permissions"}

    async def test_create_doctor_of_appointment_returns_201(self, client, world, auth):
        r = await client.post("/api/prescriptions", json=_body(world), headers=auth(world.doctor_user))
        assert r.status_code == 201

    async def test_create_other_doctor_returns_404(self, client, world, auth, session, make_doctor):
        other = await make_doctor()
        r = await client.post("/api/prescriptions", json=_body(world), headers=auth(await _user_of(session, other)))
        assert r.status_code == 404
        assert r.json() == {"detail": "Appointment not found"}
        assert await _count(session) == 0

    async def test_create_nonexistent_appointment_returns_404(self, client, world, auth):
        r = await client.post(
            "/api/prescriptions", json=_body(world, appointment_id=999999), headers=auth(world.doctor_user)
        )
        assert r.status_code == 404
        assert r.json() == {"detail": "Appointment not found"}

    async def test_create_doctor_without_profile_returns_403(self, client, world, auth, make_user):
        ghost = await make_user(UserRole.DOCTOR)
        r = await client.post("/api/prescriptions", json=_body(world), headers=auth(ghost))
        assert r.status_code == 403
        assert r.json() == {"detail": "Doctor profile not found"}

    async def test_list_treating_doctor_returns_200(self, client, world, auth, session, now):
        await _make_prescription(session, world.appt, now)
        r = await client.get(f"/api/appointments/{world.appt.id}/prescriptions", headers=auth(world.doctor_user))
        assert r.status_code == 200
        assert len(r.json()) == 1

    async def test_list_other_doctor_who_never_treated_returns_404(self, client, world, auth, session, make_doctor):
        other = await make_doctor()
        r = await client.get(
            f"/api/appointments/{world.appt.id}/prescriptions", headers=auth(await _user_of(session, other))
        )
        assert r.status_code == 404
        assert r.json() == {"detail": "Appointment not found"}

    async def test_list_other_doctor_who_treated_same_patient_sees_prescriptions(
        self, client, world, auth, session, make_doctor, now
    ):
        other = await make_doctor()
        await _make_appointment(session, other, world.patient, world.registrar)
        await _make_prescription(session, world.appt, now, medication="Чужой рецепт")
        r = await client.get(
            f"/api/appointments/{world.appt.id}/prescriptions", headers=auth(await _user_of(session, other))
        )
        assert r.status_code == 200
        assert [x["medication"] for x in r.json()] == ["Чужой рецепт"]

    @pytest.mark.parametrize(
        "status", [AppointmentStatus.SCHEDULED, AppointmentStatus.CANCELLED, AppointmentStatus.NO_SHOW]
    )
    async def test_list_doctor_with_only_not_completed_visit_returns_404(
        self, client, session, make_doctor, make_user, auth, status
    ):
        doctor = await make_doctor()
        reg = await make_user(UserRole.REGISTRAR)
        patient = await _make_patient(session)
        appt = await _make_appointment(session, doctor, patient, reg, status=status)
        r = await client.get(
            f"/api/appointments/{appt.id}/prescriptions", headers=auth(await _user_of(session, doctor))
        )
        assert r.status_code == 404

    async def test_list_scheduled_appointment_of_treated_patient_returns_empty(self, client, world, auth, session):
        scheduled = await _make_appointment(
            session, world.doctor, world.patient, world.registrar, status=AppointmentStatus.SCHEDULED
        )
        r = await client.get(f"/api/appointments/{scheduled.id}/prescriptions", headers=auth(world.doctor_user))
        assert r.status_code == 200
        assert r.json() == []

    async def test_list_own_patient_returns_200(self, client, world, auth, session, now):
        await _make_prescription(session, world.appt, now)
        r = await client.get(f"/api/appointments/{world.appt.id}/prescriptions", headers=auth(world.patient_user))
        assert r.status_code == 200
        assert len(r.json()) == 1

    async def test_list_foreign_patient_returns_404(self, client, world, auth, session, make_user, now):
        await _make_prescription(session, world.appt, now)
        stranger = await make_user(UserRole.PATIENT)
        await _make_patient(session, stranger)
        r = await client.get(f"/api/appointments/{world.appt.id}/prescriptions", headers=auth(stranger))
        assert r.status_code == 404
        assert r.json() == {"detail": "Appointment not found"}

    async def test_list_patient_without_profile_returns_404(self, client, world, auth, make_user):
        orphan = await make_user(UserRole.PATIENT)
        r = await client.get(f"/api/appointments/{world.appt.id}/prescriptions", headers=auth(orphan))
        assert r.status_code == 404

    @pytest.mark.parametrize("role", ["doctor", "patient"])
    async def test_list_nonexistent_appointment_returns_404(self, client, world, auth, role):
        user = world.doctor_user if role == "doctor" else world.patient_user
        r = await client.get("/api/appointments/999999/prescriptions", headers=auth(user))
        assert r.status_code == 404
        assert r.json() == {"detail": "Appointment not found"}

    async def test_patient_sees_own_cancelled_appointment_prescriptions(self, client, world, auth, session):
        cancelled = await _make_appointment(
            session, world.doctor, world.patient, world.registrar, status=AppointmentStatus.CANCELLED
        )
        r = await client.get(f"/api/appointments/{cancelled.id}/prescriptions", headers=auth(world.patient_user))
        assert r.status_code == 200
        assert r.json() == []


# --------------------------------------------------------------------------- #
# Создание
# --------------------------------------------------------------------------- #


class TestCreate:
    async def test_create_returns_201_with_body_and_persists(self, client, world, auth, session, now):
        body = _body(world, instructions="После еды")
        r = await client.post("/api/prescriptions", json=body, headers=auth(world.doctor_user))
        assert r.status_code == 201
        data = r.json()
        assert set(data) == {"id", "appointment_id", "medication", "dosage", "instructions", "issued_at"}
        assert data["appointment_id"] == world.appt.id
        assert data["medication"] == "Амоксициллин"
        assert data["dosage"] == "500 мг 3 раза в день"
        assert data["instructions"] == "После еды"
        assert datetime.fromisoformat(data["issued_at"]) == now
        stored = await session.get(models.Prescription, data["id"])
        assert (stored.appointment_id, stored.medication, stored.dosage, stored.instructions) == (
            world.appt.id,
            "Амоксициллин",
            "500 мг 3 раза в день",
            "После еды",
        )
        assert stored.issued_at == now

    async def test_create_without_instructions_stores_null(self, client, world, auth, session):
        r = await client.post("/api/prescriptions", json=_body(world), headers=auth(world.doctor_user))
        assert r.status_code == 201
        assert r.json()["instructions"] is None
        assert (await session.get(models.Prescription, r.json()["id"])).instructions is None

    async def test_create_strips_whitespace(self, client, world, auth):
        r = await client.post(
            "/api/prescriptions",
            json=_body(world, medication="  Ибупрофен ", dosage=" 200 мг ", instructions=" вечером "),
            headers=auth(world.doctor_user),
        )
        assert r.status_code == 201
        assert (r.json()["medication"], r.json()["dosage"], r.json()["instructions"]) == (
            "Ибупрофен",
            "200 мг",
            "вечером",
        )

    async def test_two_prescriptions_for_one_appointment_are_allowed(self, client, world, auth, session):
        for med in ("А", "Б"):
            r = await client.post(
                "/api/prescriptions", json=_body(world, medication=med), headers=auth(world.doctor_user)
            )
            assert r.status_code == 201
        assert await _count(session) == 2

    @pytest.mark.parametrize(
        "status", [AppointmentStatus.SCHEDULED, AppointmentStatus.CANCELLED, AppointmentStatus.NO_SHOW]
    )
    async def test_create_for_not_completed_appointment_returns_409(self, client, world, auth, session, status):
        appt = await _make_appointment(session, world.doctor, world.patient, world.registrar, status=status)
        r = await client.post(
            "/api/prescriptions", json=_body(world, appointment_id=appt.id), headers=auth(world.doctor_user)
        )
        assert r.status_code == 409
        assert r.json() == {"detail": "Prescriptions can only be issued for completed appointments"}
        assert await _count(session) == 0


class TestCreateValidation:
    @pytest.mark.parametrize(
        "field,length,expected",
        [
            ("medication", 1, 201),
            ("medication", 255, 201),
            ("medication", 256, 422),
            ("dosage", 1, 201),
            ("dosage", 100, 201),
            ("dosage", 101, 422),
            ("instructions", 1, 201),
            ("instructions", 2000, 201),
            ("instructions", 2001, 422),
        ],
        ids=lambda v: str(v),
    )
    async def test_length_boundaries(self, client, world, auth, field, length, expected):
        r = await client.post(
            "/api/prescriptions", json=_body(world, **{field: "я" * length}), headers=auth(world.doctor_user)
        )
        assert r.status_code == expected

    @pytest.mark.parametrize("field", ["medication", "dosage", "instructions"])
    @pytest.mark.parametrize("value", ["", "   "], ids=["empty", "blank"])
    async def test_empty_text_returns_422(self, client, world, auth, session, field, value):
        r = await client.post(
            "/api/prescriptions", json=_body(world, **{field: value}), headers=auth(world.doctor_user)
        )
        assert r.status_code == 422
        assert await _count(session) == 0

    @pytest.mark.parametrize("field", ["medication", "dosage"])
    @pytest.mark.parametrize("value", [None, 5, ["x"]], ids=["null", "int", "list"])
    async def test_required_text_wrong_type_returns_422(self, client, world, auth, field, value):
        r = await client.post(
            "/api/prescriptions", json=_body(world, **{field: value}), headers=auth(world.doctor_user)
        )
        assert r.status_code == 422

    @pytest.mark.parametrize("missing", ["appointment_id", "medication", "dosage"])
    async def test_missing_required_field_returns_422(self, client, world, auth, missing):
        body = _body(world)
        del body[missing]
        r = await client.post("/api/prescriptions", json=body, headers=auth(world.doctor_user))
        assert r.status_code == 422
        assert any(missing in e["loc"] for e in r.json()["detail"])

    async def test_instructions_null_is_accepted(self, client, world, auth):
        r = await client.post(
            "/api/prescriptions", json=_body(world, instructions=None), headers=auth(world.doctor_user)
        )
        assert r.status_code == 201

    @pytest.mark.parametrize("extra", [{"issued_at": "2020-01-01T00:00:00Z"}, {"id": 5}, {"doctor_id": 1}])
    async def test_extra_field_returns_422(self, client, world, auth, extra):
        r = await client.post("/api/prescriptions", json=_body(world, **extra), headers=auth(world.doctor_user))
        assert r.status_code == 422

    @pytest.mark.parametrize(
        "value", [0, -1, 2_147_483_648, "abc", 1.5, None], ids=["zero", "neg", "overflow", "str", "float", "null"]
    )
    async def test_bad_appointment_id_returns_422(self, client, world, auth, value):
        r = await client.post(
            "/api/prescriptions", json=_body(world, appointment_id=value), headers=auth(world.doctor_user)
        )
        assert r.status_code == 422

    async def test_no_update_or_delete_endpoints(self, client, world, auth, session, now):
        p = await _make_prescription(session, world.appt, now)
        for method in ("patch", "put", "delete"):
            r = await getattr(client, method)(f"/api/prescriptions/{p.id}", headers=auth(world.doctor_user))
            assert r.status_code in (404, 405)
        assert await _count(session) == 1


# --------------------------------------------------------------------------- #
# Чтение
# --------------------------------------------------------------------------- #


class TestList:
    async def test_list_returns_full_items(self, client, world, auth, session, now):
        p = await _make_prescription(session, world.appt, now, medication="М", dosage="Д", instructions="И")
        r = await client.get(f"/api/appointments/{world.appt.id}/prescriptions", headers=auth(world.patient_user))
        assert r.status_code == 200
        item = r.json()[0]
        assert item["id"] == p.id
        assert item["appointment_id"] == world.appt.id
        assert (item["medication"], item["dosage"], item["instructions"]) == ("М", "Д", "И")
        assert datetime.fromisoformat(item["issued_at"]) == now

    async def test_list_sorted_by_issue_time_then_id(self, client, world, auth, session, now):
        late = await _make_prescription(session, world.appt, now, medication="поздний")
        first = await _make_prescription(session, world.appt, now - timedelta(hours=1), medication="ранний")
        tie1 = await _make_prescription(session, world.appt, now + timedelta(hours=1), medication="t1")
        tie2 = await _make_prescription(session, world.appt, now + timedelta(hours=1), medication="t2")
        r = await client.get(f"/api/appointments/{world.appt.id}/prescriptions", headers=auth(world.doctor_user))
        assert [x["id"] for x in r.json()] == [first.id, late.id, tie1.id, tie2.id]

    async def test_list_contains_only_requested_appointment(self, client, world, auth, session, now):
        other = await _make_appointment(session, world.doctor, world.patient, world.registrar)
        await _make_prescription(session, world.appt, now, medication="моя")
        await _make_prescription(session, other, now, medication="другой приём")
        r = await client.get(f"/api/appointments/{world.appt.id}/prescriptions", headers=auth(world.doctor_user))
        assert [x["medication"] for x in r.json()] == ["моя"]

    async def test_list_empty_returns_empty_list(self, client, world, auth):
        r = await client.get(f"/api/appointments/{world.appt.id}/prescriptions", headers=auth(world.doctor_user))
        assert r.status_code == 200
        assert r.json() == []

    @pytest.mark.parametrize("aid", ["0", "-1", "abc", "2147483648"])
    async def test_list_bad_appointment_id_returns_422(self, client, world, auth, aid):
        r = await client.get(f"/api/appointments/{aid}/prescriptions", headers=auth(world.doctor_user))
        assert r.status_code == 422

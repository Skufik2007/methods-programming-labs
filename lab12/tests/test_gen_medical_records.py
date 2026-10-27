"""HTTP-тесты медицинской карты: /api/medical-records и /api/patients/{id}/medical-records."""

import itertools
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app import models
from app.main import app
from app.models import AppointmentStatus, UserRole
from app.routers.appointments import get_now

_seq = itertools.count(1)
DAY = timedelta(hours=24)


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


async def _make_record(session, patient, doctor, created_at, code="J06.9", notes="заметка", appointment_id=None):
    rec = models.MedicalRecord(
        patient_id=patient.id,
        doctor_id=doctor.id,
        appointment_id=appointment_id,
        diagnosis_code=code,
        notes=notes,
        created_at=created_at,
    )
    session.add(rec)
    await session.commit()
    return rec


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
    data = {"patient_id": w.patient.id, "doctor_id": w.doctor.id, "diagnosis_code": "J06.9"}
    data.update(over)
    return data


async def _count_records(session):
    return await session.scalar(select(func.count()).select_from(models.MedicalRecord))


# --------------------------------------------------------------------------- #
# Матрица доступа
# --------------------------------------------------------------------------- #

ROLE_FORBIDDEN = pytest.mark.parametrize("role", ["admin", "registrar", "patient"])


def _headers_of(w, auth, role):
    return auth({"admin": w.admin, "registrar": w.registrar, "patient": w.patient_user}[role])


class TestAccessMatrix:
    async def test_create_without_token_returns_401(self, client, world):
        r = await client.post("/api/medical-records", json=_body(world))
        assert r.status_code == 401
        assert r.json() == {"detail": "Not authenticated"}

    async def test_list_without_token_returns_401(self, client, world):
        r = await client.get(f"/api/patients/{world.patient.id}/medical-records")
        assert r.status_code == 401

    async def test_update_without_token_returns_401(self, client, world):
        r = await client.patch("/api/medical-records/1", json={"notes": "x"})
        assert r.status_code == 401

    async def test_garbage_token_returns_401(self, client, world):
        r = await client.get(
            f"/api/patients/{world.patient.id}/medical-records", headers={"Authorization": "Bearer garbage"}
        )
        assert r.status_code == 401
        assert r.json() == {"detail": "Invalid or expired token"}

    @ROLE_FORBIDDEN
    async def test_create_non_doctor_role_returns_403_and_no_record(self, client, world, auth, session, role):
        r = await client.post("/api/medical-records", json=_body(world), headers=_headers_of(world, auth, role))
        assert r.status_code == 403
        assert r.json() == {"detail": "Insufficient permissions"}
        assert await _count_records(session) == 0

    @pytest.mark.parametrize("role", ["admin", "registrar"])
    async def test_list_staff_returns_403(self, client, world, auth, role):
        r = await client.get(
            f"/api/patients/{world.patient.id}/medical-records", headers=_headers_of(world, auth, role)
        )
        assert r.status_code == 403
        assert r.json() == {"detail": "Insufficient permissions"}

    @ROLE_FORBIDDEN
    async def test_update_non_doctor_role_returns_403(self, client, world, auth, session, role):
        rec = await _make_record(session, world.patient, world.doctor, world.appt.starts_at)
        r = await client.patch(
            f"/api/medical-records/{rec.id}", json={"notes": "x"}, headers=_headers_of(world, auth, role)
        )
        assert r.status_code == 403
        assert r.json() == {"detail": "Insufficient permissions"}

    async def test_list_treating_doctor_sees_whole_chart(self, client, world, auth, session, make_doctor):
        other = await make_doctor()
        await _make_record(session, world.patient, other, world.appt.starts_at, code="I10")
        await _make_record(session, world.patient, world.doctor, world.appt.starts_at + timedelta(hours=1))
        r = await client.get(f"/api/patients/{world.patient.id}/medical-records", headers=auth(world.doctor_user))
        assert r.status_code == 200
        assert sorted(x["diagnosis_code"] for x in r.json()) == ["I10", "J06.9"]

    async def test_list_other_doctor_returns_404(self, client, world, auth, session, make_doctor):
        other = await make_doctor()
        await _make_record(session, world.patient, world.doctor, world.appt.starts_at)
        r = await client.get(
            f"/api/patients/{world.patient.id}/medical-records", headers=auth(await _user_of(session, other))
        )
        assert r.status_code == 404
        assert r.json() == {"detail": "Patient not found"}

    @pytest.mark.parametrize(
        "status", [AppointmentStatus.SCHEDULED, AppointmentStatus.CANCELLED, AppointmentStatus.NO_SHOW]
    )
    async def test_list_doctor_without_completed_visit_returns_404(
        self, client, session, make_doctor, make_user, auth, status
    ):
        doctor = await make_doctor()
        reg = await make_user(UserRole.REGISTRAR)
        patient = await _make_patient(session)
        await _make_appointment(session, doctor, patient, reg, status=status)
        r = await client.get(
            f"/api/patients/{patient.id}/medical-records", headers=auth(await _user_of(session, doctor))
        )
        assert r.status_code == 404
        assert r.json() == {"detail": "Patient not found"}

    async def test_list_patient_sees_only_own_chart(self, client, world, auth, session, make_user):
        await _make_record(session, world.patient, world.doctor, world.appt.starts_at, code="J06.9")
        stranger_user = await make_user(UserRole.PATIENT)
        stranger = await _make_patient(session, stranger_user)
        await _make_record(session, stranger, world.doctor, world.appt.starts_at, code="Z00")
        r = await client.get(f"/api/patients/{world.patient.id}/medical-records", headers=auth(world.patient_user))
        assert r.status_code == 200
        assert [x["diagnosis_code"] for x in r.json()] == ["J06.9"]

    async def test_list_patient_foreign_chart_returns_404(self, client, world, auth, session, make_user):
        stranger_user = await make_user(UserRole.PATIENT)
        stranger = await _make_patient(session, stranger_user)
        r = await client.get(f"/api/patients/{stranger.id}/medical-records", headers=auth(world.patient_user))
        assert r.status_code == 404
        assert r.json() == {"detail": "Patient not found"}

    async def test_list_patient_user_without_profile_returns_404(self, client, world, auth, make_user):
        orphan = await make_user(UserRole.PATIENT)
        r = await client.get(f"/api/patients/{world.patient.id}/medical-records", headers=auth(orphan))
        assert r.status_code == 404

    @pytest.mark.parametrize("role", ["doctor", "patient"])
    async def test_list_nonexistent_patient_returns_404(self, client, world, auth, role):
        user = world.doctor_user if role == "doctor" else world.patient_user
        r = await client.get("/api/patients/999999/medical-records", headers=auth(user))
        assert r.status_code == 404
        assert r.json() == {"detail": "Patient not found"}

    async def test_update_by_treating_non_author_returns_403(self, client, world, auth, session, make_doctor):
        other = await make_doctor()
        other_user = await _user_of(session, other)
        reg = world.registrar
        await _make_appointment(session, other, world.patient, reg)  # other тоже вёл пациента
        rec = await _make_record(session, world.patient, world.doctor, world.appt.starts_at)
        r = await client.patch(f"/api/medical-records/{rec.id}", json={"notes": "чужое"}, headers=auth(other_user))
        assert r.status_code == 403
        assert r.json() == {"detail": "Only the author can modify a medical record"}
        session.expunge_all()
        rec = await session.get(models.MedicalRecord, rec.id)
        assert rec.notes == "заметка"

    async def test_update_by_doctor_who_never_treated_returns_404(self, client, world, auth, session, make_doctor):
        other = await make_doctor()
        rec = await _make_record(session, world.patient, world.doctor, world.appt.starts_at)
        r = await client.patch(
            f"/api/medical-records/{rec.id}", json={"notes": "x"}, headers=auth(await _user_of(session, other))
        )
        assert r.status_code == 404
        assert r.json() == {"detail": "Medical record not found"}

    async def test_update_nonexistent_record_returns_404(self, client, world, auth):
        r = await client.patch("/api/medical-records/999999", json={"notes": "x"}, headers=auth(world.doctor_user))
        assert r.status_code == 404
        assert r.json() == {"detail": "Medical record not found"}


# --------------------------------------------------------------------------- #
# Создание
# --------------------------------------------------------------------------- #


class TestCreate:
    async def test_create_with_appointment_returns_201_and_persists(self, client, world, auth, session, now):
        body = _body(world, appointment_id=world.appt.id, notes="Кашель, температура")
        r = await client.post("/api/medical-records", json=body, headers=auth(world.doctor_user))
        assert r.status_code == 201
        data = r.json()
        assert data["patient_id"] == world.patient.id
        assert data["doctor_id"] == world.doctor.id
        assert data["appointment_id"] == world.appt.id
        assert data["diagnosis_code"] == "J06.9"
        assert data["notes"] == "Кашель, температура"
        assert datetime.fromisoformat(data["created_at"]) == now
        rec = await session.get(models.MedicalRecord, data["id"])
        assert (rec.patient_id, rec.doctor_id, rec.appointment_id, rec.diagnosis_code, rec.notes) == (
            world.patient.id,
            world.doctor.id,
            world.appt.id,
            "J06.9",
            "Кашель, температура",
        )
        assert rec.created_at == now

    async def test_create_without_appointment_for_treated_patient_returns_201(self, client, world, auth, session):
        r = await client.post("/api/medical-records", json=_body(world), headers=auth(world.doctor_user))
        assert r.status_code == 201
        assert r.json()["appointment_id"] is None
        assert r.json()["notes"] is None
        assert await _count_records(session) == 1

    async def test_create_lowercase_code_is_normalized_to_upper(self, client, world, auth, session):
        r = await client.post(
            "/api/medical-records", json=_body(world, diagnosis_code=" j06.9 "), headers=auth(world.doctor_user)
        )
        assert r.status_code == 201
        assert r.json()["diagnosis_code"] == "J06.9"

    async def test_create_notes_are_stripped(self, client, world, auth):
        r = await client.post(
            "/api/medical-records", json=_body(world, notes="  текст  "), headers=auth(world.doctor_user)
        )
        assert r.status_code == 201
        assert r.json()["notes"] == "текст"

    async def test_create_doctor_id_of_another_doctor_returns_403(self, client, world, auth, session, make_doctor):
        other = await make_doctor()
        r = await client.post(
            "/api/medical-records", json=_body(world, doctor_id=other.id), headers=auth(world.doctor_user)
        )
        assert r.status_code == 403
        assert r.json() == {"detail": "doctor_id must match the current doctor"}
        assert await _count_records(session) == 0

    async def test_create_by_doctor_without_profile_returns_403(self, client, world, auth, make_user):
        ghost = await make_user(UserRole.DOCTOR)
        r = await client.post("/api/medical-records", json=_body(world), headers=auth(ghost))
        assert r.status_code == 403
        assert r.json() == {"detail": "Doctor profile not found"}

    async def test_create_appointment_of_other_doctor_returns_404(self, client, world, auth, session, make_doctor):
        other = await make_doctor()
        foreign = await _make_appointment(session, other, world.patient, world.registrar)
        r = await client.post(
            "/api/medical-records", json=_body(world, appointment_id=foreign.id), headers=auth(world.doctor_user)
        )
        assert r.status_code == 404
        assert r.json() == {"detail": "Appointment not found"}
        assert await _count_records(session) == 0

    async def test_create_nonexistent_appointment_returns_404(self, client, world, auth):
        r = await client.post(
            "/api/medical-records", json=_body(world, appointment_id=999999), headers=auth(world.doctor_user)
        )
        assert r.status_code == 404
        assert r.json() == {"detail": "Appointment not found"}

    async def test_create_appointment_of_other_patient_returns_422(self, client, world, auth, session):
        other_patient = await _make_patient(session)
        foreign = await _make_appointment(session, world.doctor, other_patient, world.registrar)
        r = await client.post(
            "/api/medical-records", json=_body(world, appointment_id=foreign.id), headers=auth(world.doctor_user)
        )
        assert r.status_code == 422
        assert r.json() == {"detail": "The appointment belongs to a different patient"}
        assert await _count_records(session) == 0

    @pytest.mark.parametrize(
        "status", [AppointmentStatus.SCHEDULED, AppointmentStatus.CANCELLED, AppointmentStatus.NO_SHOW]
    )
    async def test_create_linked_to_not_completed_appointment_returns_409(self, client, world, auth, session, status):
        appt = await _make_appointment(session, world.doctor, world.patient, world.registrar, status=status)
        r = await client.post(
            "/api/medical-records", json=_body(world, appointment_id=appt.id), headers=auth(world.doctor_user)
        )
        assert r.status_code == 409
        assert r.json() == {"detail": "Medical records can only be linked to completed appointments"}
        assert await _count_records(session) == 0

    async def test_create_without_appointment_for_untreated_patient_returns_404(self, client, world, auth, session):
        stranger = await _make_patient(session)
        r = await client.post(
            "/api/medical-records", json=_body(world, patient_id=stranger.id), headers=auth(world.doctor_user)
        )
        assert r.status_code == 404
        assert r.json() == {"detail": "Patient not found"}
        assert await _count_records(session) == 0

    async def test_create_for_nonexistent_patient_returns_404(self, client, world, auth):
        r = await client.post(
            "/api/medical-records", json=_body(world, patient_id=999999), headers=auth(world.doctor_user)
        )
        assert r.status_code == 404
        assert r.json() == {"detail": "Patient not found"}


class TestCreateValidation:
    @pytest.mark.parametrize("code", ["J06", "J06.9", "j06.99", "I10", "S72.00", "A00.0"])
    async def test_valid_icd10_codes_are_accepted(self, client, world, auth, code):
        r = await client.post(
            "/api/medical-records", json=_body(world, diagnosis_code=code), headers=auth(world.doctor_user)
        )
        assert r.status_code == 201
        assert r.json()["diagnosis_code"] == code.upper()

    @pytest.mark.parametrize(
        "code",
        ["", "J6", "J", "06", "JJ06", "J06.", "J06.999", "J06,9", "J0699", "Ж06.9", "J06.9 *", "J06.9\n x"],
        ids=lambda c: repr(c),
    )
    async def test_invalid_icd10_code_returns_422(self, client, world, auth, session, code):
        r = await client.post(
            "/api/medical-records", json=_body(world, diagnosis_code=code), headers=auth(world.doctor_user)
        )
        assert r.status_code == 422
        assert await _count_records(session) == 0

    @pytest.mark.parametrize("value", [None, 5, ["J06"]], ids=["null", "int", "list"])
    async def test_diagnosis_code_wrong_type_returns_422(self, client, world, auth, value):
        r = await client.post(
            "/api/medical-records", json=_body(world, diagnosis_code=value), headers=auth(world.doctor_user)
        )
        assert r.status_code == 422

    @pytest.mark.parametrize("missing", ["patient_id", "doctor_id", "diagnosis_code"])
    async def test_missing_required_field_returns_422(self, client, world, auth, missing):
        body = _body(world)
        del body[missing]
        r = await client.post("/api/medical-records", json=body, headers=auth(world.doctor_user))
        assert r.status_code == 422
        assert any(missing in e["loc"] for e in r.json()["detail"])

    async def test_extra_field_returns_422(self, client, world, auth):
        r = await client.post(
            "/api/medical-records",
            json=_body(world, created_at="2020-01-01T00:00:00Z"),
            headers=auth(world.doctor_user),
        )
        assert r.status_code == 422

    @pytest.mark.parametrize("field", ["patient_id", "doctor_id", "appointment_id"])
    @pytest.mark.parametrize(
        "value", [0, -1, 2_147_483_648, "abc", 1.5], ids=["zero", "neg", "overflow", "str", "float"]
    )
    async def test_bad_id_values_return_422(self, client, world, auth, field, value):
        r = await client.post(
            "/api/medical-records", json=_body(world, **{field: value}), headers=auth(world.doctor_user)
        )
        assert r.status_code == 422

    async def test_max_int_id_is_valid_type_but_unknown_patient_returns_404(self, client, world, auth):
        r = await client.post(
            "/api/medical-records", json=_body(world, patient_id=2_147_483_647), headers=auth(world.doctor_user)
        )
        assert r.status_code == 404

    @pytest.mark.parametrize("length,expected", [(1, 201), (10_000, 201), (10_001, 422)], ids=["min", "max", "max+1"])
    async def test_notes_length_boundaries(self, client, world, auth, length, expected):
        r = await client.post(
            "/api/medical-records", json=_body(world, notes="а" * length), headers=auth(world.doctor_user)
        )
        assert r.status_code == expected

    @pytest.mark.parametrize("notes", ["", "   "], ids=["empty", "blank"])
    async def test_empty_notes_return_422(self, client, world, auth, notes):
        r = await client.post("/api/medical-records", json=_body(world, notes=notes), headers=auth(world.doctor_user))
        assert r.status_code == 422

    async def test_notes_null_is_accepted(self, client, world, auth):
        r = await client.post("/api/medical-records", json=_body(world, notes=None), headers=auth(world.doctor_user))
        assert r.status_code == 201
        assert r.json()["notes"] is None

    async def test_non_json_body_returns_422(self, client, world, auth):
        r = await client.post(
            "/api/medical-records",
            content=b"not json",
            headers={**auth(world.doctor_user), "Content-Type": "application/json"},
        )
        assert r.status_code == 422


# --------------------------------------------------------------------------- #
# Чтение: порядок и пагинация
# --------------------------------------------------------------------------- #


class TestList:
    async def test_list_empty_chart_returns_empty_list(self, client, world, auth):
        r = await client.get(f"/api/patients/{world.patient.id}/medical-records", headers=auth(world.patient_user))
        assert r.status_code == 200
        assert r.json() == []

    async def test_list_returns_all_fields(self, client, world, auth, session):
        rec = await _make_record(
            session, world.patient, world.doctor, world.appt.starts_at, notes="n", appointment_id=world.appt.id
        )
        r = await client.get(f"/api/patients/{world.patient.id}/medical-records", headers=auth(world.patient_user))
        item = r.json()[0]
        assert set(item) == {"id", "patient_id", "doctor_id", "appointment_id", "diagnosis_code", "notes", "created_at"}
        assert item["id"] == rec.id
        assert item["appointment_id"] == world.appt.id
        assert item["notes"] == "n"
        assert datetime.fromisoformat(item["created_at"]) == world.appt.starts_at

    async def test_list_orders_newest_first_with_id_tiebreak(self, client, world, auth, session, now):
        a = await _make_record(session, world.patient, world.doctor, now - timedelta(days=2), code="A00")
        b = await _make_record(session, world.patient, world.doctor, now - timedelta(days=1), code="B00")
        c = await _make_record(session, world.patient, world.doctor, now - timedelta(days=1), code="C00")
        r = await client.get(f"/api/patients/{world.patient.id}/medical-records", headers=auth(world.doctor_user))
        assert [x["id"] for x in r.json()] == [c.id, b.id, a.id]

    async def _seed(self, session, world, now, n=5):
        return [
            await _make_record(session, world.patient, world.doctor, now - timedelta(hours=i), code=f"A0{i}")
            for i in range(n)
        ]

    @pytest.mark.parametrize(
        "limit,offset,expected",
        [(2, 0, ["A00", "A01"]), (2, 2, ["A02", "A03"]), (2, 4, ["A04"]), (10, 5, []), (1, 0, ["A00"]), (100, 0, None)],
        ids=["first", "second", "tail", "past-end", "limit-min", "limit-max"],
    )
    async def test_list_pagination(self, client, world, auth, session, now, limit, offset, expected):
        await self._seed(session, world, now)
        r = await client.get(
            f"/api/patients/{world.patient.id}/medical-records",
            params={"limit": limit, "offset": offset},
            headers=auth(world.doctor_user),
        )
        assert r.status_code == 200
        codes = [x["diagnosis_code"] for x in r.json()]
        assert codes == (expected if expected is not None else [f"A0{i}" for i in range(5)])

    @pytest.mark.parametrize(
        "params",
        [{"limit": 0}, {"limit": 101}, {"limit": -1}, {"offset": -1}, {"offset": 2_147_483_648}, {"limit": "x"}],
        ids=["limit0", "limit101", "limit-neg", "offset-neg", "offset-overflow", "limit-str"],
    )
    async def test_list_bad_pagination_returns_422(self, client, world, auth, params):
        r = await client.get(
            f"/api/patients/{world.patient.id}/medical-records", params=params, headers=auth(world.doctor_user)
        )
        assert r.status_code == 422

    @pytest.mark.parametrize("pid", ["0", "-1", "abc", "2147483648"])
    async def test_list_bad_patient_id_returns_422(self, client, world, auth, pid):
        r = await client.get(f"/api/patients/{pid}/medical-records", headers=auth(world.doctor_user))
        assert r.status_code == 422


# --------------------------------------------------------------------------- #
# Правка
# --------------------------------------------------------------------------- #


class TestUpdate:
    async def test_author_updates_both_fields(self, client, world, auth, session, now):
        rec = await _make_record(session, world.patient, world.doctor, now - timedelta(hours=1))
        r = await client.patch(
            f"/api/medical-records/{rec.id}",
            json={"diagnosis_code": "i10", "notes": "новая"},
            headers=auth(world.doctor_user),
        )
        assert r.status_code == 200
        assert r.json()["diagnosis_code"] == "I10"
        assert r.json()["notes"] == "новая"
        assert r.json()["id"] == rec.id
        session.expunge_all()
        fresh = await session.get(models.MedicalRecord, rec.id)
        assert (fresh.diagnosis_code, fresh.notes) == ("I10", "новая")

    async def test_partial_update_keeps_other_field(self, client, world, auth, session, now):
        rec = await _make_record(session, world.patient, world.doctor, now, code="J06.9", notes="старая")
        r = await client.patch(
            f"/api/medical-records/{rec.id}", json={"notes": "новая"}, headers=auth(world.doctor_user)
        )
        assert r.status_code == 200
        assert (r.json()["diagnosis_code"], r.json()["notes"]) == ("J06.9", "новая")

    async def test_notes_can_be_cleared_with_null(self, client, world, auth, session, now):
        rec = await _make_record(session, world.patient, world.doctor, now, notes="старая")
        r = await client.patch(f"/api/medical-records/{rec.id}", json={"notes": None}, headers=auth(world.doctor_user))
        assert r.status_code == 200
        assert r.json()["notes"] is None
        session.expunge_all()
        assert (await session.get(models.MedicalRecord, rec.id)).notes is None

    async def test_update_does_not_change_created_at_or_author(self, client, world, auth, session, now):
        created = now - timedelta(hours=5)
        rec = await _make_record(session, world.patient, world.doctor, created)
        r = await client.patch(f"/api/medical-records/{rec.id}", json={"notes": "x"}, headers=auth(world.doctor_user))
        assert datetime.fromisoformat(r.json()["created_at"]) == created
        assert r.json()["doctor_id"] == world.doctor.id

    @pytest.mark.parametrize(
        "age,expected",
        [
            (DAY - timedelta(seconds=1), 200),
            (DAY, 200),
            (DAY + timedelta(seconds=1), 403),
            (DAY + timedelta(days=30), 403),
        ],
        ids=["24h-1s", "exactly-24h", "24h+1s", "month"],
    )
    async def test_edit_window_boundary(self, client, world, auth, session, now, age, expected):
        rec = await _make_record(session, world.patient, world.doctor, now - age, notes="старая")
        r = await client.patch(
            f"/api/medical-records/{rec.id}", json={"notes": "новая"}, headers=auth(world.doctor_user)
        )
        assert r.status_code == expected
        session.expunge_all()
        stored = (await session.get(models.MedicalRecord, rec.id)).notes
        if expected == 403:
            assert r.json() == {"detail": "A medical record can only be modified within 24 hours after creation"}
            assert stored == "старая"
        else:
            assert stored == "новая"

    async def test_edit_window_follows_get_now_override(self, client, world, auth, session, now):
        rec = await _make_record(session, world.patient, world.doctor, now)
        app.dependency_overrides[get_now] = lambda: now + DAY + timedelta(seconds=1)
        r = await client.patch(f"/api/medical-records/{rec.id}", json={"notes": "x"}, headers=auth(world.doctor_user))
        assert r.status_code == 403

    async def test_record_created_at_future_is_editable(self, client, world, auth, session, now):
        rec = await _make_record(session, world.patient, world.doctor, now + timedelta(hours=1))
        r = await client.patch(f"/api/medical-records/{rec.id}", json={"notes": "x"}, headers=auth(world.doctor_user))
        assert r.status_code == 200

    async def test_expired_record_by_non_author_still_reports_authorship(
        self, client, world, auth, session, now, make_doctor
    ):
        other = await make_doctor()
        await _make_appointment(session, other, world.patient, world.registrar)
        rec = await _make_record(session, world.patient, world.doctor, now - timedelta(days=3))
        r = await client.patch(
            f"/api/medical-records/{rec.id}", json={"notes": "x"}, headers=auth(await _user_of(session, other))
        )
        assert r.status_code == 403
        assert r.json() == {"detail": "Only the author can modify a medical record"}

    async def test_empty_body_returns_422_with_detail(self, client, world, auth, session, now):
        rec = await _make_record(session, world.patient, world.doctor, now)
        r = await client.patch(f"/api/medical-records/{rec.id}", json={}, headers=auth(world.doctor_user))
        assert r.status_code == 422
        assert r.json() == {"detail": "No fields to update"}

    async def test_null_diagnosis_code_returns_422(self, client, world, auth, session, now):
        rec = await _make_record(session, world.patient, world.doctor, now)
        r = await client.patch(
            f"/api/medical-records/{rec.id}", json={"diagnosis_code": None}, headers=auth(world.doctor_user)
        )
        assert r.status_code == 422
        session.expunge_all()
        assert (await session.get(models.MedicalRecord, rec.id)).diagnosis_code == "J06.9"

    @pytest.mark.parametrize(
        "body",
        [
            {"patient_id": 2},
            {"doctor_id": 2},
            {"appointment_id": 1},
            {"notes": "x", "created_at": "2020-01-01T00:00:00Z"},
            {"diagnosis_code": "bad"},
            {"notes": ""},
            {"notes": "а" * 10_001},
        ],
        ids=["patient_id", "doctor_id", "appointment_id", "created_at", "bad-code", "empty-notes", "long-notes"],
    )
    async def test_invalid_patch_body_returns_422(self, client, world, auth, session, now, body):
        rec = await _make_record(session, world.patient, world.doctor, now)
        r = await client.patch(f"/api/medical-records/{rec.id}", json=body, headers=auth(world.doctor_user))
        assert r.status_code == 422

    @pytest.mark.parametrize("rid", ["0", "-1", "abc", "2147483648"])
    async def test_bad_record_id_returns_422(self, client, world, auth, rid):
        r = await client.patch(f"/api/medical-records/{rid}", json={"notes": "x"}, headers=auth(world.doctor_user))
        assert r.status_code == 422

    async def test_no_delete_endpoint(self, client, world, auth, session, now):
        rec = await _make_record(session, world.patient, world.doctor, now)
        r = await client.delete(f"/api/medical-records/{rec.id}", headers=auth(world.doctor_user))
        assert r.status_code == 405
        assert await _count_records(session) == 1

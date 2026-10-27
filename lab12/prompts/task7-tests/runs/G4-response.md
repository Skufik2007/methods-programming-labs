# G4: тесты медкарты, назначений, аналитики, main и errors

Прогон: 413 тестов, все проходят (SQLite). Файлы кладутся в `tests/`; `conftest.py` не менялся.

## Матрица доступа (из кода)

| Эндпоинт | без токена | admin | registrar | doctor (свой) | doctor (чужой) | patient (владелец) | patient (чужой) |
|---|---|---|---|---|---|---|---|
| POST /api/medical-records | 401 | 403 | 403 | 201 (404 если не вёл пациента или чужая запись; 403 при чужом doctor_id) | - | 403 | 403 |
| GET /api/patients/{id}/medical-records | 401 | 403 | 403 | 200 (был completed-приём) | 404 | 200 | 404 |
| PATCH /api/medical-records/{id} | 401 | 403 | 403 | 200 автор, до 24 ч включительно | 403 если вёл пациента, иначе 404 | 403 | 403 |
| POST /api/prescriptions | 401 | 403 | 403 | 201 (409 если не completed) | 404 | 403 | 403 |
| GET /api/appointments/{id}/prescriptions | 401 | 403 | 403 | 200 | 404 | 200 | 404 |
| GET /api/analytics/* (4 шт.) | 401 | 200 | 200 | 403 | - | 403 | - |

### tests/test_gen_medical_records.py

```python
"""HTTP-тесты медицинской карты: /api/medical-records и /api/patients/{id}/medical-records."""

import itertools
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app import models
from app.models import AppointmentStatus, UserRole
from app.routers.appointments import get_now
from app.main import app

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
        session.expunge_all(); rec = await session.get(models.MedicalRecord, rec.id)
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
    async def test_create_linked_to_not_completed_appointment_returns_409(
        self, client, world, auth, session, status
    ):
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
            "/api/medical-records", json=_body(world, created_at="2020-01-01T00:00:00Z"), headers=auth(world.doctor_user)
        )
        assert r.status_code == 422

    @pytest.mark.parametrize("field", ["patient_id", "doctor_id", "appointment_id"])
    @pytest.mark.parametrize("value", [0, -1, 2_147_483_648, "abc", 1.5], ids=["zero", "neg", "overflow", "str", "float"])
    async def test_bad_id_values_return_422(self, client, world, auth, field, value):
        r = await client.post("/api/medical-records", json=_body(world, **{field: value}), headers=auth(world.doctor_user))
        assert r.status_code == 422

    async def test_max_int_id_is_valid_type_but_unknown_patient_returns_404(self, client, world, auth):
        r = await client.post(
            "/api/medical-records", json=_body(world, patient_id=2_147_483_647), headers=auth(world.doctor_user)
        )
        assert r.status_code == 404

    @pytest.mark.parametrize(
        "length,expected", [(1, 201), (10_000, 201), (10_001, 422)], ids=["min", "max", "max+1"]
    )
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
            "/api/medical-records", content=b"not json", headers={**auth(world.doctor_user), "Content-Type": "application/json"}
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
        r = await client.patch(f"/api/medical-records/{rec.id}", json={"notes": "новая"}, headers=auth(world.doctor_user))
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
```

### tests/test_gen_prescriptions.py

```python
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
        r = await client.post(
            "/api/prescriptions", json=_body(world), headers=auth(await _user_of(session, other))
        )
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

    async def test_list_scheduled_appointment_of_treated_patient_returns_empty(
        self, client, world, auth, session
    ):
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
            r = await client.post("/api/prescriptions", json=_body(world, medication=med), headers=auth(world.doctor_user))
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
        r = await client.post("/api/prescriptions", json=_body(world, **{field: value}), headers=auth(world.doctor_user))
        assert r.status_code == 422
        assert await _count(session) == 0

    @pytest.mark.parametrize("field", ["medication", "dosage"])
    @pytest.mark.parametrize("value", [None, 5, ["x"]], ids=["null", "int", "list"])
    async def test_required_text_wrong_type_returns_422(self, client, world, auth, field, value):
        r = await client.post("/api/prescriptions", json=_body(world, **{field: value}), headers=auth(world.doctor_user))
        assert r.status_code == 422

    @pytest.mark.parametrize("missing", ["appointment_id", "medication", "dosage"])
    async def test_missing_required_field_returns_422(self, client, world, auth, missing):
        body = _body(world)
        del body[missing]
        r = await client.post("/api/prescriptions", json=body, headers=auth(world.doctor_user))
        assert r.status_code == 422
        assert any(missing in e["loc"] for e in r.json()["detail"])

    async def test_instructions_null_is_accepted(self, client, world, auth):
        r = await client.post("/api/prescriptions", json=_body(world, instructions=None), headers=auth(world.doctor_user))
        assert r.status_code == 201

    @pytest.mark.parametrize("extra", [{"issued_at": "2020-01-01T00:00:00Z"}, {"id": 5}, {"doctor_id": 1}])
    async def test_extra_field_returns_422(self, client, world, auth, extra):
        r = await client.post("/api/prescriptions", json=_body(world, **extra), headers=auth(world.doctor_user))
        assert r.status_code == 422

    @pytest.mark.parametrize("value", [0, -1, 2_147_483_648, "abc", 1.5, None], ids=["zero", "neg", "overflow", "str", "float", "null"])
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
```

### tests/test_gen_analytics.py

```python
"""HTTP-тесты аналитики /api/analytics/* на данных с заранее известными числами."""

import itertools
from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from app import models
from app.models import AppointmentStatus as S
from app.models import UserRole

MSK = timezone(timedelta(hours=3))
_seq = itertools.count(1)

ENDPOINTS = ["summary", "doctors-load", "appointments-per-day", "top-diagnoses"]
WEEK = {"date_from": "2026-10-12", "date_to": "2026-10-18"}  # пн-вс


def msk(day: int, hour: int = 0, minute: int = 0, month: int = 10) -> datetime:
    """Местное время клиники (МСК) 2026 года как aware UTC."""
    return datetime(2026, month, day, hour, minute, tzinfo=MSK).astimezone(UTC)


async def _user_of(session, doctor):
    return await session.get(models.User, doctor.user_id)


async def _patient(session):
    p = models.Patient(full_name="Пациент", birth_date=date(1990, 1, 1), policy_number=f"{next(_seq):016d}")
    session.add(p)
    await session.commit()
    return p


async def _appt(session, doctor, patient, creator, start, minutes=30, status=S.COMPLETED):
    a = models.Appointment(
        doctor_id=doctor.id,
        patient_id=patient.id,
        starts_at=start,
        ends_at=start + timedelta(minutes=minutes),
        status=status,
        created_by=creator.id,
    )
    session.add(a)
    await session.commit()
    return a


async def _record(session, patient, doctor, created_at, code):
    session.add(
        models.MedicalRecord(
            patient_id=patient.id, doctor_id=doctor.id, diagnosis_code=code, notes="секрет", created_at=created_at
        )
    )
    await session.commit()


@pytest.fixture
async def staff(make_user):
    return {"admin": await make_user(UserRole.ADMIN), "registrar": await make_user(UserRole.REGISTRAR)}


@pytest.fixture
async def reg(staff):
    return staff["registrar"]


@pytest.fixture
async def hdr(staff, auth):
    return auth(staff["admin"])


# --------------------------------------------------------------------------- #
# Матрица доступа
# --------------------------------------------------------------------------- #


class TestAccessMatrix:
    @pytest.mark.parametrize("endpoint", ENDPOINTS)
    async def test_without_token_returns_401(self, client, endpoint):
        r = await client.get(f"/api/analytics/{endpoint}", params=WEEK)
        assert r.status_code == 401
        assert r.json() == {"detail": "Not authenticated"}

    @pytest.mark.parametrize("endpoint", ENDPOINTS)
    @pytest.mark.parametrize("role", [UserRole.DOCTOR, UserRole.PATIENT])
    async def test_medical_roles_get_403(self, client, make_user, make_doctor, auth, endpoint, role):
        user = await make_user(role)
        r = await client.get(f"/api/analytics/{endpoint}", params=WEEK, headers=auth(user))
        assert r.status_code == 403
        assert r.json() == {"detail": "Insufficient permissions"}

    @pytest.mark.parametrize("endpoint", ENDPOINTS)
    @pytest.mark.parametrize("role", ["admin", "registrar"])
    async def test_staff_roles_get_200(self, client, staff, auth, endpoint, role):
        r = await client.get(f"/api/analytics/{endpoint}", params=WEEK, headers=auth(staff[role]))
        assert r.status_code == 200

    @pytest.mark.parametrize("endpoint", ENDPOINTS)
    async def test_forbidden_role_is_checked_before_validation(self, client, make_user, auth, endpoint):
        patient = await make_user(UserRole.PATIENT)
        r = await client.get(f"/api/analytics/{endpoint}", headers=auth(patient))  # без дат
        assert r.status_code == 403


# --------------------------------------------------------------------------- #
# Валидация параметров
# --------------------------------------------------------------------------- #


class TestValidation:
    @pytest.mark.parametrize("endpoint", ENDPOINTS)
    @pytest.mark.parametrize(
        "params",
        [
            {},
            {"date_from": "2026-10-12"},
            {"date_to": "2026-10-18"},
            {"date_from": "12.10.2026", "date_to": "18.10.2026"},
            {"date_from": "abc", "date_to": "2026-10-18"},
            {"date_from": "2026-02-30", "date_to": "2026-10-18"},
            {"date_from": "2026-10-12", "date_to": ""},
            {"date_from": "1969-12-31", "date_to": "1970-01-05"},
            {"date_from": "2100-12-25", "date_to": "2101-01-01"},
        ],
        ids=["none", "no-to", "no-from", "dotted", "word", "feb30", "empty-to", "before-1970", "after-2100"],
    )
    async def test_bad_dates_return_422(self, client, hdr, endpoint, params):
        r = await client.get(f"/api/analytics/{endpoint}", params=params, headers=hdr)
        assert r.status_code == 422

    @pytest.mark.parametrize("endpoint", ENDPOINTS)
    async def test_date_from_after_date_to_returns_422_with_detail(self, client, hdr, endpoint):
        r = await client.get(
            f"/api/analytics/{endpoint}", params={"date_from": "2026-10-13", "date_to": "2026-10-12"}, headers=hdr
        )
        assert r.status_code == 422
        assert r.json() == {"detail": "date_from must not be later than date_to"}

    @pytest.mark.parametrize("endpoint", ENDPOINTS)
    async def test_period_of_367_days_returns_422(self, client, hdr, endpoint):
        r = await client.get(
            f"/api/analytics/{endpoint}", params={"date_from": "2026-01-01", "date_to": "2027-01-02"}, headers=hdr
        )
        assert r.status_code == 422
        assert r.json() == {"detail": "The period must not be longer than 366 days"}

    @pytest.mark.parametrize("endpoint", ENDPOINTS)
    async def test_period_of_366_days_is_accepted(self, client, hdr, endpoint):
        r = await client.get(
            f"/api/analytics/{endpoint}", params={"date_from": "2026-01-01", "date_to": "2027-01-01"}, headers=hdr
        )
        assert r.status_code == 200

    async def test_leap_year_366_day_period_is_accepted_and_367_rejected(self, client, hdr):
        ok = await client.get(
            "/api/analytics/appointments-per-day",
            params={"date_from": "2028-01-01", "date_to": "2028-12-31"},
            headers=hdr,
        )
        assert ok.status_code == 200
        assert len(ok.json()) == 366
        bad = await client.get(
            "/api/analytics/appointments-per-day",
            params={"date_from": "2028-01-01", "date_to": "2029-01-01"},
            headers=hdr,
        )
        assert bad.status_code == 422

    @pytest.mark.parametrize("endpoint", ENDPOINTS)
    async def test_single_day_period_is_accepted(self, client, hdr, endpoint):
        r = await client.get(
            f"/api/analytics/{endpoint}", params={"date_from": "2026-10-12", "date_to": "2026-10-12"}, headers=hdr
        )
        assert r.status_code == 200

    @pytest.mark.parametrize("endpoint", ENDPOINTS)
    async def test_boundary_dates_1970_and_2100_are_accepted(self, client, hdr, endpoint):
        r = await client.get(
            f"/api/analytics/{endpoint}", params={"date_from": "1970-01-01", "date_to": "1970-01-02"}, headers=hdr
        )
        assert r.status_code == 200
        r = await client.get(
            f"/api/analytics/{endpoint}", params={"date_from": "2100-12-30", "date_to": "2100-12-31"}, headers=hdr
        )
        assert r.status_code == 200

    @pytest.mark.parametrize(
        "limit,expected", [(1, 200), (10, 200), (50, 200), (0, 422), (51, 422), (-1, 422), ("x", 422)]
    )
    async def test_top_diagnoses_limit_boundaries(self, client, hdr, limit, expected):
        r = await client.get("/api/analytics/top-diagnoses", params={**WEEK, "limit": limit}, headers=hdr)
        assert r.status_code == expected


# --------------------------------------------------------------------------- #
# Сводка
# --------------------------------------------------------------------------- #


@pytest.fixture
async def dataset(session, make_doctor, reg):
    """D1 (пн-пт 09-18, 540 мин/день) и D2 (пн-ср 10-14, 240 мин/день); неделя 12-18.10.2026.

    D1: пн 09:00 completed 30, 09:30 completed 30, 10:00 no_show 30, 10:30 cancelled 30,
        ср 09:00 scheduled 60.
    D2: вт 10:00 completed 120, вт 12:00 cancelled 30.
    Вне периода: вс 11.10 23:30 и пн 19.10 00:00 (местное время).
    """
    d1 = await make_doctor()
    d2 = await make_doctor(work_start=datetime(2000, 1, 1, 10).time(), work_end=datetime(2000, 1, 1, 14).time(), work_days=[1, 2, 3])
    p = await _patient(session)
    rows = [
        (d1, msk(12, 9, 0), 30, S.COMPLETED),
        (d1, msk(12, 9, 30), 30, S.COMPLETED),
        (d1, msk(12, 10, 0), 30, S.NO_SHOW),
        (d1, msk(12, 10, 30), 30, S.CANCELLED),
        (d1, msk(14, 9, 0), 60, S.SCHEDULED),
        (d2, msk(13, 10, 0), 120, S.COMPLETED),
        (d2, msk(13, 12, 0), 30, S.CANCELLED),
        (d1, msk(11, 23, 30), 30, S.COMPLETED),  # до периода
        (d1, msk(19, 0, 0), 30, S.NO_SHOW),  # после периода
    ]
    for doctor, start, minutes, status in rows:
        await _appt(session, doctor, p, reg, start, minutes, status)
    return d1, d2


class TestSummary:
    async def test_summary_counts_and_rates(self, client, hdr, dataset):
        r = await client.get("/api/analytics/summary", params=WEEK, headers=hdr)
        assert r.status_code == 200
        assert r.json() == {
            "date_from": "2026-10-12",
            "date_to": "2026-10-18",
            "total": 7,
            "by_status": {"scheduled": 1, "completed": 3, "cancelled": 2, "no_show": 1},
            "no_show_rate": 0.25,  # 1 / (3 + 1)
            "cancellation_rate": 0.2857,  # 2 / 7
        }

    async def test_summary_excludes_appointments_outside_period_by_local_date(self, client, hdr, dataset):
        r = await client.get(
            "/api/analytics/summary", params={"date_from": "2026-10-11", "date_to": "2026-10-11"}, headers=hdr
        )
        assert r.json()["total"] == 1
        assert r.json()["by_status"]["completed"] == 1

    async def test_summary_period_end_is_inclusive_and_next_midnight_excluded(self, client, hdr, dataset):
        r = await client.get(
            "/api/analytics/summary", params={"date_from": "2026-10-19", "date_to": "2026-10-19"}, headers=hdr
        )
        assert r.json()["total"] == 1
        assert r.json()["by_status"]["no_show"] == 1
        r = await client.get(
            "/api/analytics/summary", params={"date_from": "2026-10-12", "date_to": "2026-10-12"}, headers=hdr
        )
        assert r.json()["total"] == 4

    async def test_summary_empty_period_returns_zeros(self, client, hdr):
        r = await client.get("/api/analytics/summary", params=WEEK, headers=hdr)
        assert r.json() == {
            "date_from": "2026-10-12",
            "date_to": "2026-10-18",
            "total": 0,
            "by_status": {"scheduled": 0, "completed": 0, "cancelled": 0, "no_show": 0},
            "no_show_rate": 0.0,
            "cancellation_rate": 0.0,
        }

    async def test_summary_only_scheduled_gives_zero_no_show_rate(self, client, hdr, session, make_doctor, reg):
        d = await make_doctor()
        p = await _patient(session)
        await _appt(session, d, p, reg, msk(12, 9), status=S.SCHEDULED)
        r = await client.get("/api/analytics/summary", params=WEEK, headers=hdr)
        assert r.json()["total"] == 1
        assert r.json()["no_show_rate"] == 0.0
        assert r.json()["cancellation_rate"] == 0.0

    async def test_summary_only_no_show_gives_rate_one(self, client, hdr, session, make_doctor, reg):
        d = await make_doctor()
        p = await _patient(session)
        await _appt(session, d, p, reg, msk(12, 9), status=S.NO_SHOW)
        r = await client.get("/api/analytics/summary", params=WEEK, headers=hdr)
        assert r.json()["no_show_rate"] == 1.0

    async def test_summary_only_cancelled_gives_cancellation_rate_one_and_no_show_zero(
        self, client, hdr, session, make_doctor, reg
    ):
        d = await make_doctor()
        p = await _patient(session)
        await _appt(session, d, p, reg, msk(12, 9), status=S.CANCELLED)
        r = await client.get("/api/analytics/summary", params=WEEK, headers=hdr)
        assert r.json()["cancellation_rate"] == 1.0
        assert r.json()["no_show_rate"] == 0.0

    async def test_summary_rate_rounds_to_four_digits(self, client, hdr, session, make_doctor, reg):
        d = await make_doctor()
        p = await _patient(session)
        for i, st in enumerate([S.COMPLETED, S.COMPLETED, S.NO_SHOW]):
            await _appt(session, d, p, reg, msk(12, 9 + i), status=st)
        r = await client.get("/api/analytics/summary", params=WEEK, headers=hdr)
        assert r.json()["no_show_rate"] == 0.3333  # 1 / 3

    async def test_summary_contains_no_personal_data(self, client, hdr, dataset):
        r = await client.get("/api/analytics/summary", params=WEEK, headers=hdr)
        assert set(r.json()) == {"date_from", "date_to", "total", "by_status", "no_show_rate", "cancellation_rate"}


# --------------------------------------------------------------------------- #
# Загрузка врачей
# --------------------------------------------------------------------------- #


class TestDoctorsLoad:
    async def test_load_values_for_known_data(self, client, hdr, dataset):
        d1, d2 = dataset
        r = await client.get("/api/analytics/doctors-load", params=WEEK, headers=hdr)
        assert r.status_code == 200
        assert r.json() == [
            {
                "doctor_id": d1.id,
                "specialty": "Терапевт",
                "is_active": True,
                "available_minutes": 2700,  # 5 дней * 540
                "busy_minutes": 150,  # 30 + 30 + 30 + 60, отменённая не считается
                "load_percent": 5.56,  # 150 / 2700 * 100
                "completed": 2,
                "no_show": 1,
            },
            {
                "doctor_id": d2.id,
                "specialty": "Терапевт",
                "is_active": True,
                "available_minutes": 720,  # 3 дня * 240
                "busy_minutes": 120,
                "load_percent": 16.67,
                "completed": 1,
                "no_show": 0,
            },
        ]

    async def test_load_available_minutes_for_two_week_period(self, client, hdr, dataset):
        d1, d2 = dataset
        r = await client.get(
            "/api/analytics/doctors-load", params={"date_from": "2026-10-12", "date_to": "2026-10-25"}, headers=hdr
        )
        by_id = {x["doctor_id"]: x for x in r.json()}
        assert by_id[d1.id]["available_minutes"] == 5400
        assert by_id[d2.id]["available_minutes"] == 1440

    async def test_load_available_minutes_for_weekend_only_is_zero(self, client, hdr, dataset):
        d1, _ = dataset
        r = await client.get(
            "/api/analytics/doctors-load", params={"date_from": "2026-10-17", "date_to": "2026-10-18"}, headers=hdr
        )
        assert r.json()[0]["available_minutes"] == 0
        assert r.json()[0]["load_percent"] == 0.0

    async def test_load_available_minutes_partial_week_counts_only_working_weekdays(self, client, hdr, dataset):
        d1, d2 = dataset
        # чт 15.10 - вс 18.10: у D1 два рабочих дня (чт, пт), у D2 - ни одного
        r = await client.get(
            "/api/analytics/doctors-load", params={"date_from": "2026-10-15", "date_to": "2026-10-18"}, headers=hdr
        )
        by_id = {x["doctor_id"]: x for x in r.json()}
        assert by_id[d1.id]["available_minutes"] == 1080
        assert by_id[d2.id]["available_minutes"] == 0

    async def test_load_over_100_percent_is_not_capped(self, client, hdr, session, make_doctor, reg):
        d = await make_doctor()
        p = await _patient(session)
        await _appt(session, d, p, reg, msk(12, 9), minutes=600, status=S.COMPLETED)
        r = await client.get(
            "/api/analytics/doctors-load", params={"date_from": "2026-10-12", "date_to": "2026-10-12"}, headers=hdr
        )
        assert r.json()[0]["busy_minutes"] == 600
        assert r.json()[0]["available_minutes"] == 540
        assert r.json()[0]["load_percent"] == 111.11

    async def test_load_exactly_full_day_is_100_percent(self, client, hdr, session, make_doctor, reg):
        d = await make_doctor()
        p = await _patient(session)
        await _appt(session, d, p, reg, msk(12, 9), minutes=540, status=S.SCHEDULED)
        r = await client.get(
            "/api/analytics/doctors-load", params={"date_from": "2026-10-12", "date_to": "2026-10-12"}, headers=hdr
        )
        assert r.json()[0]["load_percent"] == 100.0

    async def test_load_doctor_without_workdays_has_zero_percent_not_error(
        self, client, hdr, session, make_doctor, reg
    ):
        d = await make_doctor(work_days=[])
        p = await _patient(session)
        await _appt(session, d, p, reg, msk(12, 9), status=S.COMPLETED)
        r = await client.get("/api/analytics/doctors-load", params=WEEK, headers=hdr)
        assert r.status_code == 200
        assert r.json()[0]["available_minutes"] == 0
        assert r.json()[0]["busy_minutes"] == 30
        assert r.json()[0]["load_percent"] == 0.0

    async def test_load_doctor_without_appointments_has_zeros(self, client, hdr, make_doctor):
        d = await make_doctor()
        r = await client.get("/api/analytics/doctors-load", params=WEEK, headers=hdr)
        assert r.json() == [
            {
                "doctor_id": d.id,
                "specialty": "Терапевт",
                "is_active": True,
                "available_minutes": 2700,
                "busy_minutes": 0,
                "load_percent": 0.0,
                "completed": 0,
                "no_show": 0,
            }
        ]

    async def test_load_includes_deactivated_doctor_with_flag(self, client, hdr, session, make_doctor):
        d = await make_doctor()
        user = await _user_of(session, d)
        user.is_active = False
        await session.commit()
        r = await client.get("/api/analytics/doctors-load", params=WEEK, headers=hdr)
        assert [x["is_active"] for x in r.json()] == [False]

    async def test_load_cancelled_does_not_add_busy_minutes(self, client, hdr, session, make_doctor, reg):
        d = await make_doctor()
        p = await _patient(session)
        await _appt(session, d, p, reg, msk(12, 9), minutes=60, status=S.CANCELLED)
        r = await client.get("/api/analytics/doctors-load", params=WEEK, headers=hdr)
        assert r.json()[0]["busy_minutes"] == 0

    async def test_load_empty_without_doctors_returns_empty_list(self, client, hdr):
        r = await client.get("/api/analytics/doctors-load", params=WEEK, headers=hdr)
        assert r.status_code == 200
        assert r.json() == []

    async def test_load_response_has_no_names(self, client, hdr, dataset):
        r = await client.get("/api/analytics/doctors-load", params=WEEK, headers=hdr)
        for item in r.json():
            assert "full_name" not in item
            assert set(item) == {
                "doctor_id", "specialty", "is_active", "available_minutes", "busy_minutes",
                "load_percent", "completed", "no_show",
            }


# --------------------------------------------------------------------------- #
# Записи по дням
# --------------------------------------------------------------------------- #


class TestPerDay:
    @pytest.fixture
    async def edge_data(self, session, make_doctor, reg):
        d = await make_doctor()
        p = await _patient(session)
        rows = [
            (msk(11, 23, 30), S.COMPLETED),  # 11.10 - вне периода
            (msk(12, 0, 0), S.CANCELLED),  # 12.10 00:00 местное (= 11.10 21:00 UTC) - первый день
            (msk(12, 9, 0), S.SCHEDULED),
            (msk(14, 9, 0), S.NO_SHOW),
            (msk(18, 23, 30), S.COMPLETED),  # последний день, 20:30 UTC
            (msk(19, 0, 0), S.COMPLETED),  # 19.10 00:00 - вне периода (граница не включается)
        ]
        for start, st in rows:
            await _appt(session, d, p, reg, start, status=st)

    async def test_per_day_counts_all_statuses_by_local_date(self, client, hdr, edge_data):
        r = await client.get("/api/analytics/appointments-per-day", params=WEEK, headers=hdr)
        assert r.status_code == 200
        assert r.json() == [
            {"day": "2026-10-12", "count": 2},
            {"day": "2026-10-13", "count": 0},
            {"day": "2026-10-14", "count": 1},
            {"day": "2026-10-15", "count": 0},
            {"day": "2026-10-16", "count": 0},
            {"day": "2026-10-17", "count": 0},
            {"day": "2026-10-18", "count": 1},
        ]

    async def test_per_day_days_without_appointments_are_present_with_zero(self, client, hdr, edge_data):
        r = await client.get("/api/analytics/appointments-per-day", params=WEEK, headers=hdr)
        assert [x["day"] for x in r.json()] == [f"2026-10-{d}" for d in range(12, 19)]
        assert sum(1 for x in r.json() if x["count"] == 0) == 4

    async def test_per_day_single_day_midnight_belongs_to_local_date(self, client, hdr, edge_data):
        r = await client.get(
            "/api/analytics/appointments-per-day",
            params={"date_from": "2026-10-12", "date_to": "2026-10-12"},
            headers=hdr,
        )
        assert r.json() == [{"day": "2026-10-12", "count": 2}]

    async def test_per_day_late_evening_stays_on_same_local_date(self, client, hdr, edge_data):
        r = await client.get(
            "/api/analytics/appointments-per-day",
            params={"date_from": "2026-10-11", "date_to": "2026-10-11"},
            headers=hdr,
        )
        assert r.json() == [{"day": "2026-10-11", "count": 1}]

    async def test_per_day_empty_period_returns_zero_for_each_day(self, client, hdr):
        r = await client.get("/api/analytics/appointments-per-day", params=WEEK, headers=hdr)
        assert len(r.json()) == 7
        assert all(x["count"] == 0 for x in r.json())

    async def test_per_day_366_days_returns_366_items_in_order(self, client, hdr):
        r = await client.get(
            "/api/analytics/appointments-per-day",
            params={"date_from": "2026-01-01", "date_to": "2027-01-01"},
            headers=hdr,
        )
        days = [x["day"] for x in r.json()]
        assert len(days) == 366
        assert days[0] == "2026-01-01"
        assert days[-1] == "2027-01-01"
        assert days == sorted(days)


# --------------------------------------------------------------------------- #
# Топ диагнозов
# --------------------------------------------------------------------------- #


class TestTopDiagnoses:
    @pytest.fixture
    async def records(self, session, make_doctor):
        d = await make_doctor()
        p = await _patient(session)
        plan = [
            ("J06.9", msk(12, 9)),
            ("J06.9", msk(13, 9)),
            ("J06.9", msk(18, 23, 59)),
            ("I10", msk(12, 0, 0)),  # ровно начало периода
            ("I10", msk(15, 9)),
            ("A09", msk(14, 9)),
            ("A09", msk(16, 9)),
            ("K21", msk(17, 9)),
            ("J06.9", msk(11, 23, 59)),  # вне периода
            ("Z00", msk(19, 0, 0)),  # вне периода (следующая полночь)
        ]
        for code, when in plan:
            await _record(session, p, d, when, code)

    async def test_top_diagnoses_values_and_tie_order_by_code(self, client, hdr, records):
        r = await client.get("/api/analytics/top-diagnoses", params=WEEK, headers=hdr)
        assert r.status_code == 200
        assert r.json() == [
            {"code": "J06.9", "count": 3},
            {"code": "A09", "count": 2},  # при равенстве - по коду: A09 раньше I10
            {"code": "I10", "count": 2},
            {"code": "K21", "count": 1},
        ]

    @pytest.mark.parametrize(
        "limit,expected_codes",
        [(1, ["J06.9"]), (2, ["J06.9", "A09"]), (3, ["J06.9", "A09", "I10"]), (4, ["J06.9", "A09", "I10", "K21"]), (50, ["J06.9", "A09", "I10", "K21"])],
    )
    async def test_top_diagnoses_limit(self, client, hdr, records, limit, expected_codes):
        r = await client.get("/api/analytics/top-diagnoses", params={**WEEK, "limit": limit}, headers=hdr)
        assert [x["code"] for x in r.json()] == expected_codes

    async def test_top_diagnoses_default_limit_is_ten(self, client, hdr, session, make_doctor):
        d = await make_doctor()
        p = await _patient(session)
        for i in range(12):
            await _record(session, p, d, msk(12, 9), f"B{i:02d}")
        r = await client.get("/api/analytics/top-diagnoses", params=WEEK, headers=hdr)
        assert len(r.json()) == 10

    async def test_top_diagnoses_exposes_only_code_and_count(self, client, hdr, records):
        r = await client.get("/api/analytics/top-diagnoses", params=WEEK, headers=hdr)
        for item in r.json():
            assert set(item) == {"code", "count"}
        assert "секрет" not in r.text

    async def test_top_diagnoses_empty_period_returns_empty_list(self, client, hdr, records):
        r = await client.get(
            "/api/analytics/top-diagnoses", params={"date_from": "2026-11-01", "date_to": "2026-11-02"}, headers=hdr
        )
        assert r.status_code == 200
        assert r.json() == []

    async def test_top_diagnoses_period_boundaries_are_by_created_at_local_date(self, client, hdr, records):
        r = await client.get(
            "/api/analytics/top-diagnoses", params={"date_from": "2026-10-19", "date_to": "2026-10-19"}, headers=hdr
        )
        assert r.json() == [{"code": "Z00", "count": 1}]
```

### tests/test_gen_main_errors.py

```python
"""Тесты app/main.py (заголовки безопасности, index.html, статика) и app/errors.py."""

import itertools
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.exc import IntegrityError

from app import models
from app.errors import (
    BusinessRuleError,
    ConflictError,
    DomainError,
    NotFoundError,
    PermissionDeniedError,
    is_unique_violation,
    register_exception_handlers,
)
from app.main import STATIC_DIR
from app.models import AppointmentStatus, UserRole

_seq = itertools.count(1)

CSP_STRICT_EXPECTED = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'"
)
APP_DIR = Path(STATIC_DIR).parent


# --------------------------------------------------------------------------- #
# Заголовки безопасности
# --------------------------------------------------------------------------- #

STRICT_PATHS = [
    "/",
    "/static/app.js",
    "/static/app.css",
    "/static/missing.js",
    "/api/health",
    "/api/doctors",  # 401 без токена
    "/api/analytics/summary",  # 422/401
    "/openapi.json",
    "/docsx",
    "/api/docs",
    "/nonexistent",
]


class TestSecurityHeaders:
    @pytest.mark.parametrize("path", STRICT_PATHS)
    async def test_strict_csp_everywhere_except_docs(self, client, path):
        r = await client.get(path)
        assert r.headers["content-security-policy"] == CSP_STRICT_EXPECTED

    @pytest.mark.parametrize("path", STRICT_PATHS + ["/docs", "/redoc"])
    async def test_nosniff_and_referrer_policy_on_every_response(self, client, path):
        r = await client.get(path)
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["referrer-policy"] == "no-referrer"

    @pytest.mark.parametrize("path", ["/docs", "/redoc"])
    async def test_docs_pages_get_relaxed_csp(self, client, path):
        r = await client.get(path)
        assert r.status_code == 200
        csp = r.headers["content-security-policy"]
        assert "https://cdn.jsdelivr.net" in csp
        assert "'unsafe-inline'" in csp
        assert "frame-ancestors 'none'" in csp
        assert csp != CSP_STRICT_EXPECTED

    async def test_docs_with_query_string_still_relaxed(self, client):
        r = await client.get("/docs?x=1")
        assert "'unsafe-inline'" in r.headers["content-security-policy"]

    async def test_strict_csp_has_no_unsafe_directives_or_external_hosts(self, client):
        csp = (await client.get("/")).headers["content-security-policy"]
        assert "unsafe" not in csp
        assert "http" not in csp
        assert "*" not in csp

    async def test_openapi_json_is_not_relaxed(self, client):
        r = await client.get("/openapi.json")
        assert r.status_code == 200
        assert "'unsafe-inline'" not in r.headers["content-security-policy"]

    async def test_headers_on_401_response(self, client):
        r = await client.get("/api/doctors")
        assert r.status_code == 401
        assert r.headers["content-security-policy"] == CSP_STRICT_EXPECTED
        assert r.headers["x-content-type-options"] == "nosniff"

    async def test_headers_on_422_response(self, client, make_user, auth):
        admin = await make_user(UserRole.ADMIN)
        r = await client.get("/api/analytics/summary", headers=auth(admin))
        assert r.status_code == 422
        assert r.headers["content-security-policy"] == CSP_STRICT_EXPECTED

    async def test_headers_on_405_response(self, client):
        r = await client.post("/")
        assert r.status_code == 405
        assert r.headers["content-security-policy"] == CSP_STRICT_EXPECTED

    async def test_headers_on_domain_error_response(self, client, make_user, auth):
        doctor_user = await make_user(UserRole.DOCTOR)
        r = await client.get("/api/patients/1/medical-records", headers=auth(doctor_user))
        assert r.status_code == 404
        assert r.headers["content-security-policy"] == CSP_STRICT_EXPECTED
        assert r.headers["referrer-policy"] == "no-referrer"


# --------------------------------------------------------------------------- #
# index.html и статика
# --------------------------------------------------------------------------- #


class TestIndexAndStatic:
    async def test_index_returns_html_with_no_cache(self, client):
        r = await client.get("/")
        assert r.status_code == 200
        assert r.headers["content-type"] == "text/html; charset=utf-8"
        assert r.headers["cache-control"] == "no-cache"
        assert r.content == (STATIC_DIR / "index.html").read_bytes()
        assert "<title>Система управления клиникой</title>" in r.text

    async def test_index_is_served_without_authentication(self, client):
        assert (await client.get("/")).status_code == 200

    async def test_index_not_in_openapi_schema(self, client):
        schema = (await client.get("/openapi.json")).json()
        assert "/" not in schema["paths"]

    async def test_index_has_no_inline_scripts_or_styles(self, client):
        import re

        html = (await client.get("/")).text
        for tag in re.findall(r"<script\b[^>]*>", html):
            assert "src=" in tag
        assert "<style" not in html
        assert " style=" not in html
        assert not re.search(r"\son[a-z]+=", html)  # onclick= и т.п. запрещены строгой CSP

    async def test_index_references_only_local_resources(self, client):
        import re

        html = (await client.get("/")).text
        for url in re.findall(r'(?:src|href)="([^"]*)"', html):
            assert url.startswith(("/static/", "#", "data:")), url

    @pytest.mark.parametrize(
        "name,content_type_part",
        [("app.js", "javascript"), ("app.css", "text/css")],
    )
    async def test_static_files_are_served_with_content(self, client, name, content_type_part):
        r = await client.get(f"/static/{name}")
        assert r.status_code == 200
        assert content_type_part in r.headers["content-type"]
        assert r.content == (STATIC_DIR / name).read_bytes()

    async def test_static_index_is_reachable_under_static_too(self, client):
        r = await client.get("/static/index.html")
        assert r.status_code == 200
        assert r.content == (STATIC_DIR / "index.html").read_bytes()

    async def test_missing_static_file_returns_404_json(self, client):
        r = await client.get("/static/nope.js")
        assert r.status_code == 404
        assert r.json() == {"detail": "Not Found"}

    async def test_static_directory_listing_is_not_exposed(self, client):
        r = await client.get("/static/")
        assert r.status_code == 404

    @pytest.mark.parametrize(
        "path",
        ["/static/../main.py", "/static/%2e%2e/main.py", "/static/..%2fmain.py", "/static/%2e%2e%2fconfig.py"],
        ids=["dotdot", "enc-dots", "enc-slash", "enc-both"],
    )
    async def test_static_path_traversal_does_not_leak_sources(self, client, path):
        r = await client.get(path)
        assert r.status_code == 404
        assert "get_settings" not in r.text and "create_app" not in r.text

    async def test_health_returns_ok_without_authentication(self, client):
        r = await client.get("/api/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}

    async def test_unknown_route_returns_404_json(self, client):
        r = await client.get("/api/unknown")
        assert r.status_code == 404
        assert r.json() == {"detail": "Not Found"}


# --------------------------------------------------------------------------- #
# errors.py: классы и обработчик
# --------------------------------------------------------------------------- #


def _error_app() -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/raise/{kind}")
    async def raise_it(kind: str) -> None:
        cls = {
            "notfound": NotFoundError,
            "conflict": ConflictError,
            "denied": PermissionDeniedError,
            "rule": BusinessRuleError,
            "base": DomainError,
        }[kind]
        raise cls("текст ошибки")

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("unexpected")

    return app


@pytest.fixture
async def error_client():
    transport = httpx.ASGITransport(app=_error_app(), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


class TestDomainErrorHandler:
    @pytest.mark.parametrize(
        "kind,status",
        [("notfound", 404), ("conflict", 409), ("denied", 403), ("rule", 422), ("base", 500)],
    )
    async def test_exception_maps_to_status_and_detail(self, error_client, kind, status):
        r = await error_client.get(f"/raise/{kind}")
        assert r.status_code == status
        assert r.json() == {"detail": "текст ошибки"}
        assert r.headers["content-type"] == "application/json"

    @pytest.mark.parametrize(
        "cls,status",
        [(NotFoundError, 404), (ConflictError, 409), (PermissionDeniedError, 403), (BusinessRuleError, 422)],
    )
    def test_status_code_and_detail_attributes(self, cls, status):
        exc = cls("msg")
        assert exc.status_code == status
        assert exc.detail == "msg"
        assert str(exc) == "msg"
        assert isinstance(exc, DomainError)

    async def test_non_domain_exception_is_not_swallowed_as_domain_error(self, error_client):
        r = await error_client.get("/boom")
        assert r.status_code == 500
        assert "unexpected" not in r.text  # детали исключения не утекают клиенту

    async def test_real_api_returns_404_domain_error(self, client, make_user, auth):
        doctor_user = await make_user(UserRole.DOCTOR)
        r = await client.get("/api/appointments/999/prescriptions", headers=auth(doctor_user))
        assert (r.status_code, r.json()) == (404, {"detail": "Appointment not found"})

    async def test_real_api_returns_403_domain_error(self, client, make_user, auth):
        ghost = await make_user(UserRole.DOCTOR)  # врач без профиля
        r = await client.post(
            "/api/prescriptions", json={"appointment_id": 1, "medication": "A", "dosage": "1"}, headers=auth(ghost)
        )
        assert (r.status_code, r.json()) == (403, {"detail": "Doctor profile not found"})

    async def test_real_api_returns_422_domain_error(self, client, make_user, auth):
        admin = await make_user(UserRole.ADMIN)
        r = await client.get(
            "/api/analytics/summary", params={"date_from": "2026-10-02", "date_to": "2026-10-01"}, headers=auth(admin)
        )
        assert (r.status_code, r.json()) == (422, {"detail": "date_from must not be later than date_to"})

    async def test_real_api_returns_409_domain_error(self, client, session, make_user, make_doctor, auth):
        doctor = await make_doctor()
        doctor_user = await session.get(models.User, doctor.user_id)
        registrar = await make_user(UserRole.REGISTRAR)
        patient = models.Patient(full_name="П", birth_date=date(1990, 1, 1), policy_number=f"{next(_seq):016d}")
        session.add(patient)
        await session.commit()
        start = datetime(2026, 10, 14, 6, tzinfo=UTC)
        appt = models.Appointment(
            doctor_id=doctor.id,
            patient_id=patient.id,
            starts_at=start,
            ends_at=start + timedelta(minutes=30),
            status=AppointmentStatus.SCHEDULED,
            created_by=registrar.id,
        )
        session.add(appt)
        await session.commit()
        r = await client.post(
            "/api/prescriptions",
            json={"appointment_id": appt.id, "medication": "A", "dosage": "1"},
            headers=auth(doctor_user),
        )
        assert (r.status_code, r.json()) == (
            409,
            {"detail": "Prescriptions can only be issued for completed appointments"},
        )


# --------------------------------------------------------------------------- #
# errors.py: is_unique_violation
# --------------------------------------------------------------------------- #


def _integrity(orig) -> IntegrityError:
    return IntegrityError("INSERT ...", {}, orig)


class _Orig(Exception):
    def __init__(self, text="", **attrs):
        super().__init__(text)
        for k, v in attrs.items():
            setattr(self, k, v)


class TestIsUniqueViolation:
    @pytest.mark.parametrize(
        "orig,expected",
        [
            (_Orig(sqlstate="23505"), True),
            (_Orig(sqlstate="23P01"), True),
            (_Orig(pgcode="23505"), True),
            (_Orig(pgcode="23P01"), True),
            (_Orig(sqlstate="23503"), False),  # foreign_key_violation
            (_Orig(sqlstate="23514"), False),  # check_violation
            (_Orig(sqlstate="23502"), False),  # not_null_violation
            (_Orig(sqlite_errorcode=2067), True),
            (_Orig(sqlite_errorcode=1555), True),
            (_Orig(sqlite_errorcode=787), False),  # FOREIGN KEY
            (_Orig(sqlite_errorcode=275), False),  # CHECK
            (_Orig("UNIQUE constraint failed: users.email"), True),
            (_Orig("NOT NULL constraint failed: users.email"), False),
            (_Orig("CHECK constraint failed: x"), False),
            (_Orig(""), False),
        ],
        ids=[
            "pg-unique", "pg-exclusion", "pgcode-unique", "pgcode-exclusion", "pg-fk", "pg-check", "pg-notnull",
            "sqlite-2067", "sqlite-1555", "sqlite-fk", "sqlite-check", "text-unique", "text-notnull", "text-check",
            "empty",
        ],
    )
    def test_classification(self, orig, expected):
        assert is_unique_violation(_integrity(orig)) is expected

    def test_sqlstate_is_found_on_cause_of_wrapper(self):
        wrapper = _Orig("wrapped")
        wrapper.__cause__ = _Orig(sqlstate="23505")
        assert is_unique_violation(_integrity(wrapper)) is True

    def test_cause_with_foreign_key_state_is_not_unique(self):
        wrapper = _Orig("wrapped")
        wrapper.__cause__ = _Orig(sqlstate="23503")
        assert is_unique_violation(_integrity(wrapper)) is False

    def test_orig_none_is_not_unique(self):
        assert is_unique_violation(IntegrityError("x", {}, None)) is False

    async def test_real_duplicate_email_is_unique_violation(self, session, make_user):
        await make_user(UserRole.ADMIN, email="dup@clinic.test")
        session.add(
            models.User(email="dup@clinic.test", password_hash="x", full_name="Дубль", role=UserRole.ADMIN)
        )
        with pytest.raises(IntegrityError) as info:
            await session.commit()
        await session.rollback()
        assert is_unique_violation(info.value) is True

    async def test_real_check_violation_is_not_unique(self, session):
        session.add(models.Patient(full_name="П", birth_date=date(1990, 1, 1), policy_number="123"))
        with pytest.raises(IntegrityError) as info:
            await session.commit()
        await session.rollback()
        assert is_unique_violation(info.value) is False
```

## Что покрыто

| Файл / группа | Сценарии |
|---|---|
| test_gen_medical_records / AccessMatrix | 401 (нет и битый токен), 403 admin/registrar/patient для POST, GET, PATCH; врач видит всю карту (включая записи других врачей); другой врач, врач с scheduled/cancelled/no_show - 404; пациент свой/чужой/без профиля/несуществующий; PATCH: не автор вёл пациента - 403, не вёл - 404, нет записи - 404 |
| TestCreate | 201 с appointment_id и без него (тело + БД, created_at = now); нормализация кода и notes; 403 чужой doctor_id; 403 врач без профиля; 404 чужой/несуществующий приём; 422 приём другого пациента; 409 приём не completed (3 статуса); 404 непролеченный/несуществующий пациент |
| TestCreateValidation | валидные/невалидные коды МКБ-10, типы, обязательные поля, лишние поля, границы id (0, -1, 2^31), notes 1/10000/10001, пустые, null, не JSON |
| TestList | пусто, полный набор полей, порядок (новые первыми, tie-break по id), limit/offset (границы 1/100, за концом), 422 для limit 0/101/-1, offset -1/2^31, плохой patient_id |
| TestUpdate | оба поля, частичное, notes=null, created_at/автор не меняются, окно 24 ч (-1 с, ровно 24 ч, +1 с, месяц), get_now-override, будущий created_at, пустое тело 422, diagnosis_code=null 422, лишние/плохие поля, плохой id, DELETE отсутствует (405) |
| test_gen_prescriptions | матрица 401/403/404; 201 (тело, БД, issued_at = now, instructions null, strip, несколько рецептов); 409 для 3 статусов; границы длин medication 255/256, dosage 100/101, instructions 2000/2001; пустые, типы, лишние поля, плохой appointment_id; GET: порядок issued_at/id, фильтр по приёму, пусто, врач другого приёма того же пациента, scheduled-приём пролеченного пациента, отменённый приём пациента, 422 |
| test_gen_analytics | 401/403/200 по 4 эндпоинтам и ролям; 422: нет дат, формат, 30 февраля, до 1970 и после 2100, from > to (detail), 367 дней (detail), 366 принимается (в т.ч. високосный), один день, границы 1970/2100, limit 0/1/50/51; summary на известных числах (total 7; 3/2/1/1; 0.25; 0.2857), границы периода по местной дате, пустой период, деления на ноль, округление 0.3333; doctors-load: 2700/150/5.56 и 720/120/16.67, две недели, выходные, неполная неделя, >100% (111.11), ровно 100%, work_days=[] без ошибки, врач без записей, неактивный врач, cancelled не занимает время, нет ФИО; per-day: полночь по МСК, нули в днях без записей, 366 элементов; top-diagnoses: значения, порядок при равенстве по коду, limit, лимит по умолчанию 10, только code и count, границы по created_at |
| test_gen_main_errors / SecurityHeaders | строгая CSP для /, /static/*, /api/*, /openapi.json, 404; ослабленная только для /docs и /redoc (с query тоже); nosniff и no-referrer на 200/401/404/405/422 |
| IndexAndStatic | index.html: тип, no-cache, содержимое, без inline-скриптов/стилей/onclick, только локальные ресурсы; статика app.js/app.css; 404 JSON; листинг каталога; path traversal (4 варианта); health; неизвестный маршрут |
| errors | соответствие классов кодам 404/409/403/422/500 и тексту; необработанное исключение не утекает; все четыре кода через реальный API; is_unique_violation: 15 вариантов sqlstate/pgcode/sqlite_errorcode/текст, __cause__, orig=None, реальный дубликат email и реальное нарушение CHECK |

## Найденные ошибки приложения

Ошибок, на которые стоило бы поставить xfail, не найдено: все 413 тестов проходят, поведение соответствует докстрингам. Наблюдения без xfail:

1. `main.py`: комментарий к `CSP_DOCS` говорит о «двух страницах документации», но в `docs_paths` попадает и `swagger_ui_oauth2_redirect_url` (`/docs/oauth2-redirect`), то есть ослабленная CSP (с `'unsafe-inline'`) выдаётся трём путям. Для работы OAuth-редиректа Swagger это нужно, но противоречит формулировке «только /docs и /redoc». Тест на `/docs/oauth2-redirect` намеренно не написан, чтобы не фиксировать спорное поведение.
2. `update_medical_record` проверяет тело (422 `No fields to update`) раньше существования записи: PATCH `{}` к несуществующей записи даёт 422, а не 404. Не ошибка, но порядок проверок зафиксирован тестом пустого тела только для существующей записи.

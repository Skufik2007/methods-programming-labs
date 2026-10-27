"""Тесты записей на приём и свободных слотов: роли, правила П1-П9, конкурентность, транзакции, время."""

import asyncio
import itertools
from datetime import UTC, datetime, time, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import db
from app.main import app
from app.models import Appointment, AppointmentStatus, Patient, User, UserRole
from app.routers.appointments import get_now
from app.schemas import AppointmentCreate
from app.services import appointments as svc

MSK = timezone(timedelta(hours=3))
FROZEN = datetime(2026, 10, 12, 6, 0, tzinfo=UTC)  # пн 12.10.2026 09:00 МСК
SCHEDULED = AppointmentStatus.SCHEDULED
COMPLETED = AppointmentStatus.COMPLETED
CANCELLED = AppointmentStatus.CANCELLED
NO_SHOW = AppointmentStatus.NO_SHOW
_policy = itertools.count(1)


# --------------------------------------------------------------------------- #
# Помощники
# --------------------------------------------------------------------------- #


def msk(day: int, hour: int, minute: int = 0, month: int = 10) -> datetime:
    """Момент по Москве (2026) -> aware UTC."""
    return datetime(2026, month, day, hour, minute, tzinfo=MSK).astimezone(UTC)


def set_now(moment: datetime) -> None:
    app.dependency_overrides[get_now] = lambda: moment


async def add_patient(session: AsyncSession, user: User | None = None) -> Patient:
    patient = Patient(
        user_id=user.id if user else None,
        full_name="Пациент Тестовый",
        birth_date=datetime(1990, 1, 1).date(),
        policy_number=f"{next(_policy):016d}",
    )
    session.add(patient)
    await session.commit()
    return patient


async def add_appt(session, doctor, patient, start, *, minutes=30, status=SCHEDULED, creator, reason=None):
    appt = Appointment(
        doctor_id=doctor.id,
        patient_id=patient.id,
        starts_at=start,
        ends_at=start + timedelta(minutes=minutes),
        status=status,
        reason=reason,
        created_by=creator.id,
    )
    session.add(appt)
    await session.commit()
    return appt


def booking(doctor, patient, start, minutes=30, **extra):
    body = {
        "doctor_id": doctor.id,
        "patient_id": patient.id,
        "starts_at": start.isoformat(),
        "ends_at": (start + timedelta(minutes=minutes)).isoformat(),
    }
    body.update(extra)
    return body


async def db_rows() -> list[Appointment]:
    async with db.SessionFactory() as s:
        return list((await s.scalars(select(Appointment).order_by(Appointment.id))).all())


async def db_appt(appt_id: int) -> Appointment:
    async with db.SessionFactory() as s:
        row = await s.get(Appointment, appt_id)
        assert row is not None
        return row


@pytest.fixture
async def w(session, make_user, make_doctor):
    ns = SimpleNamespace()
    ns.admin = await make_user(UserRole.ADMIN)
    ns.registrar = await make_user(UserRole.REGISTRAR)
    ns.doctor = await make_doctor()
    ns.doctor_user = await session.get(User, ns.doctor.user_id)
    ns.other_doctor = await make_doctor()
    ns.other_doctor_user = await session.get(User, ns.other_doctor.user_id)
    ns.patient_user = await make_user(UserRole.PATIENT)
    ns.patient = await add_patient(session, ns.patient_user)
    ns.other_patient_user = await make_user(UserRole.PATIENT)
    ns.other_patient = await add_patient(session, ns.other_patient_user)
    return ns


def actor(w, name):
    return getattr(w, name)


# --------------------------------------------------------------------------- #
# Создание: роли
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("role", ["admin", "registrar"])
async def test_create_staff_books_any_patient_returns_201_and_row(client, auth, w, role):
    user = actor(w, role)
    r = await client.post(
        "/api/appointments", json=booking(w.doctor, w.patient, msk(13, 10), reason="Боль"), headers=auth(user)
    )
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "scheduled"
    assert body["created_by"] == user.id
    assert body["doctor_id"] == w.doctor.id and body["patient_id"] == w.patient.id
    assert body["reason"] == "Боль"
    assert datetime.fromisoformat(body["starts_at"]) == msk(13, 10)
    assert datetime.fromisoformat(body["ends_at"]) == msk(13, 10, 30)
    rows = await db_rows()
    assert [(x.id, x.status, x.created_by) for x in rows] == [(body["id"], SCHEDULED, user.id)]


async def test_create_patient_for_own_profile_returns_201(client, auth, w):
    r = await client.post(
        "/api/appointments", json=booking(w.doctor, w.patient, msk(13, 10)), headers=auth(w.patient_user)
    )
    assert r.status_code == 201
    assert r.json()["created_by"] == w.patient_user.id
    assert len(await db_rows()) == 1


async def test_create_patient_for_other_profile_returns_403(client, auth, w):
    r = await client.post(
        "/api/appointments", json=booking(w.doctor, w.other_patient, msk(13, 10)), headers=auth(w.patient_user)
    )
    assert r.status_code == 403
    assert r.json() == {"detail": "Patients can only book appointments for their own profile"}
    assert await db_rows() == []


async def test_create_patient_user_without_profile_returns_403(client, auth, make_user, w):
    lonely = await make_user(UserRole.PATIENT)
    r = await client.post("/api/appointments", json=booking(w.doctor, w.patient, msk(13, 10)), headers=auth(lonely))
    assert r.status_code == 403
    assert r.json() == {"detail": "Patients can only book appointments for their own profile"}
    assert await db_rows() == []


async def test_create_doctor_role_returns_403(client, auth, w):
    r = await client.post(
        "/api/appointments", json=booking(w.doctor, w.patient, msk(13, 10)), headers=auth(w.doctor_user)
    )
    assert r.status_code == 403
    assert r.json() == {"detail": "Insufficient permissions"}
    assert await db_rows() == []


async def test_create_without_token_returns_401(client, w):
    r = await client.post("/api/appointments", json=booking(w.doctor, w.patient, msk(13, 10)))
    assert r.status_code == 401
    assert r.json() == {"detail": "Not authenticated"}


async def test_create_unknown_doctor_returns_404(client, auth, w):
    body = booking(w.doctor, w.patient, msk(13, 10))
    body["doctor_id"] = 99999
    r = await client.post("/api/appointments", json=body, headers=auth(w.admin))
    assert r.status_code == 404
    assert r.json() == {"detail": "Doctor not found"}


async def test_create_unknown_patient_returns_404(client, auth, w):
    body = booking(w.doctor, w.patient, msk(13, 10))
    body["patient_id"] = 99999
    r = await client.post("/api/appointments", json=body, headers=auth(w.admin))
    assert r.status_code == 404
    assert r.json() == {"detail": "Patient not found"}
    assert await db_rows() == []


# --------------------------------------------------------------------------- #
# Создание: П1, П2, П5, схема
# --------------------------------------------------------------------------- #

WINDOW_CASES = [
    pytest.param(13, 9, 0, 30, 201, None, id="shift_start_ok"),
    pytest.param(13, 17, 30, 30, 201, None, id="last_slot_ends_at_shift_end_ok"),
    pytest.param(16, 10, 0, 30, 201, None, id="friday_ok"),
    pytest.param(13, 9, 0, 120, 201, None, id="four_slots_ok"),
    pytest.param(13, 8, 30, 30, 422, "shift", id="starts_before_shift"),
    pytest.param(13, 18, 0, 30, 422, "shift", id="starts_at_shift_end"),
    pytest.param(13, 17, 30, 60, 422, "shift", id="ends_after_shift"),
    pytest.param(13, 9, 15, 30, 422, "slot boundary", id="start_off_grid"),
    pytest.param(13, 9, 0, 45, 422, "duration must be from 1 to 4", id="duration_not_whole_slots"),
    pytest.param(13, 9, 0, 150, 422, "duration must be from 1 to 4", id="five_slots_too_long"),
    pytest.param(17, 10, 0, 30, 422, "day of the week", id="saturday"),
    pytest.param(18, 10, 0, 30, 422, "day of the week", id="sunday"),
    pytest.param(13, 23, 30, 60, 422, "midnight", id="crosses_midnight"),
    pytest.param(12, 9, 0, 30, 422, "future", id="starts_exactly_now"),
    pytest.param(12, 8, 30, 30, 422, "future", id="starts_in_past"),
    pytest.param(12, 9, 30, 30, 201, None, id="starts_one_slot_after_now_ok"),
]


@pytest.mark.parametrize(("day", "hour", "minute", "minutes", "code", "fragment"), WINDOW_CASES)
async def test_create_window_rules(client, auth, w, day, hour, minute, minutes, code, fragment):
    r = await client.post(
        "/api/appointments", json=booking(w.doctor, w.patient, msk(day, hour, minute), minutes), headers=auth(w.admin)
    )
    assert r.status_code == code, r.text
    if fragment is None:
        assert len(await db_rows()) == 1
    else:
        assert fragment in r.json()["detail"]
        assert await db_rows() == []


@pytest.mark.parametrize(
    ("start", "code", "fragment"),
    [
        pytest.param(FROZEN + timedelta(days=90) - timedelta(minutes=30), 201, None, id="one_slot_before_limit"),
        pytest.param(FROZEN + timedelta(days=90), 201, None, id="exactly_90_days"),
        pytest.param(FROZEN + timedelta(days=90, minutes=30), 422, "at most 90 days", id="one_slot_after_limit"),
    ],
)
async def test_create_ninety_day_boundary(client, auth, make_doctor, w, start, code, fragment):
    doctor = await make_doctor(work_start=time(0), work_end=time(23, 59), work_days=[1, 2, 3, 4, 5, 6, 7])
    r = await client.post("/api/appointments", json=booking(doctor, w.patient, start), headers=auth(w.admin))
    assert r.status_code == code, r.text
    if fragment:
        assert fragment in r.json()["detail"]
        assert await db_rows() == []


async def test_create_with_deactivated_doctor_returns_422(client, auth, session, w):
    w.doctor_user.is_active = False
    await session.commit()
    r = await client.post("/api/appointments", json=booking(w.doctor, w.patient, msk(13, 10)), headers=auth(w.admin))
    assert r.status_code == 422
    assert r.json() == {"detail": "The doctor is not available for new appointments"}
    assert await db_rows() == []


async def test_create_after_clock_moved_forward_uses_new_now(client, auth, w):
    set_now(msk(13, 10))
    r = await client.post("/api/appointments", json=booking(w.doctor, w.patient, msk(13, 10)), headers=auth(w.admin))
    assert r.status_code == 422 and "future" in r.json()["detail"]
    r = await client.post(
        "/api/appointments", json=booking(w.doctor, w.patient, msk(13, 10, 30)), headers=auth(w.admin)
    )
    assert r.status_code == 201


def _body(w, **patch):
    body = booking(w.doctor, w.patient, msk(13, 10))
    body.update(patch)
    return body


@pytest.mark.parametrize(
    "patch",
    [
        pytest.param({"ends_at": msk(13, 10).isoformat()}, id="ends_equals_start"),
        pytest.param({"ends_at": msk(13, 9).isoformat()}, id="ends_before_start"),
        pytest.param({"starts_at": "2026-10-13T10:00:00", "ends_at": "2026-10-13T10:30:00"}, id="naive_datetimes"),
        pytest.param({"status": "completed"}, id="extra_field_status"),
        pytest.param({"created_by": 1}, id="extra_field_created_by"),
        pytest.param({"reason": ""}, id="empty_reason"),
        pytest.param({"reason": "я" * 501}, id="reason_501"),
        pytest.param({"doctor_id": 0}, id="doctor_id_zero"),
        pytest.param({"patient_id": 2_147_483_648}, id="patient_id_over_int"),
        pytest.param({"starts_at": "not-a-date"}, id="bad_date"),
    ],
)
async def test_create_invalid_body_returns_422_list(client, auth, w, patch):
    r = await client.post("/api/appointments", json=_body(w, **patch), headers=auth(w.admin))
    assert r.status_code == 422
    assert isinstance(r.json()["detail"], list)
    assert await db_rows() == []


async def test_create_reason_exactly_500_chars_ok_and_none_ok(client, auth, w):
    r = await client.post("/api/appointments", json=_body(w, reason="я" * 500), headers=auth(w.admin))
    assert r.status_code == 201 and len(r.json()["reason"]) == 500
    r = await client.post(
        "/api/appointments",
        json=_body(w, starts_at=msk(13, 11).isoformat(), ends_at=msk(13, 11, 30).isoformat(), reason=None),
        headers=auth(w.admin),
    )
    assert r.status_code == 201 and r.json()["reason"] is None


async def test_create_missing_required_field_returns_422(client, auth, w):
    body = _body(w)
    del body["ends_at"]
    r = await client.post("/api/appointments", json=body, headers=auth(w.admin))
    assert r.status_code == 422


# --------------------------------------------------------------------------- #
# Создание: пересечения П3, П4
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("start", "minutes", "code"),
    [
        pytest.param(msk(13, 10), 30, 409, id="same_start"),
        pytest.param(msk(13, 10, 30), 30, 409, id="inside_existing"),
        pytest.param(msk(13, 9, 30), 60, 409, id="overlaps_beginning"),
        pytest.param(msk(13, 10, 30), 60, 409, id="overlaps_end"),
        pytest.param(msk(13, 9), 30, 201, id="ends_exactly_at_existing_start"),
        pytest.param(msk(13, 9, 30), 30, 201, id="gap_before"),
        pytest.param(msk(13, 11), 30, 201, id="starts_exactly_at_existing_end"),
    ],
)
async def test_create_doctor_overlap_boundaries(client, auth, session, w, start, minutes, code):
    await add_appt(session, w.doctor, w.other_patient, msk(13, 10), minutes=60, creator=w.admin)
    r = await client.post("/api/appointments", json=booking(w.doctor, w.patient, start, minutes), headers=auth(w.admin))
    assert r.status_code == code, r.text
    if code == 409:
        assert r.json() == {"detail": "The doctor already has an appointment at this time"}
        assert len(await db_rows()) == 1
    else:
        assert len(await db_rows()) == 2


@pytest.mark.parametrize("status", [CANCELLED], ids=["cancelled"])
async def test_create_over_cancelled_appointment_is_allowed(client, auth, session, w, status):
    await add_appt(session, w.doctor, w.other_patient, msk(13, 10), status=status, creator=w.admin)
    r = await client.post("/api/appointments", json=booking(w.doctor, w.patient, msk(13, 10)), headers=auth(w.admin))
    assert r.status_code == 201


@pytest.mark.parametrize("status", [COMPLETED, NO_SHOW, SCHEDULED])
async def test_create_over_non_cancelled_appointment_conflicts(client, auth, session, w, status):
    await add_appt(session, w.doctor, w.other_patient, msk(13, 10), status=status, creator=w.admin)
    r = await client.post("/api/appointments", json=booking(w.doctor, w.patient, msk(13, 10)), headers=auth(w.admin))
    assert r.status_code == 409


async def test_create_patient_overlap_with_other_doctor_returns_409(client, auth, session, w):
    await add_appt(session, w.other_doctor, w.patient, msk(13, 10), minutes=60, creator=w.admin)
    r = await client.post(
        "/api/appointments", json=booking(w.doctor, w.patient, msk(13, 10, 30)), headers=auth(w.admin)
    )
    assert r.status_code == 409
    assert r.json() == {"detail": "The patient already has an appointment at this time"}
    assert len(await db_rows()) == 1


async def test_create_patient_adjacent_appointment_with_other_doctor_ok(client, auth, session, w):
    await add_appt(session, w.other_doctor, w.patient, msk(13, 10), creator=w.admin)
    r = await client.post(
        "/api/appointments", json=booking(w.doctor, w.patient, msk(13, 10, 30)), headers=auth(w.admin)
    )
    assert r.status_code == 201


async def test_create_patient_overlap_with_cancelled_ignored(client, auth, session, w):
    await add_appt(session, w.other_doctor, w.patient, msk(13, 10), status=CANCELLED, creator=w.admin)
    r = await client.post("/api/appointments", json=booking(w.doctor, w.patient, msk(13, 10)), headers=auth(w.admin))
    assert r.status_code == 201


# --------------------------------------------------------------------------- #
# Конкурентность
# --------------------------------------------------------------------------- #


async def test_concurrent_same_slot_different_patients_only_one_wins(client, auth, session, make_user, w):
    patients = [w.patient, w.other_patient]
    for _ in range(3):
        patients.append(await add_patient(session))
    results = await asyncio.gather(
        *(
            client.post("/api/appointments", json=booking(w.doctor, p, msk(13, 10)), headers=auth(w.admin))
            for p in patients
        )
    )
    codes = sorted(r.status_code for r in results)
    assert codes == [201, 409, 409, 409, 409]
    for r in results:
        if r.status_code == 409:
            assert r.json()["detail"] in {
                "The doctor already has an appointment at this time",
                "This time slot has just been taken; please choose another one",
            }
    rows = await db_rows()
    assert len(rows) == 1 and rows[0].status == SCHEDULED


async def test_concurrent_overlapping_intervals_only_one_wins(client, auth, w):
    a = client.post("/api/appointments", json=booking(w.doctor, w.patient, msk(13, 10), 60), headers=auth(w.admin))
    b = client.post(
        "/api/appointments", json=booking(w.doctor, w.other_patient, msk(13, 10, 30), 60), headers=auth(w.admin)
    )
    ra, rb = await asyncio.gather(a, b)
    assert sorted([ra.status_code, rb.status_code]) == [201, 409]
    assert len(await db_rows()) == 1


async def test_concurrent_adjacent_intervals_both_succeed(client, auth, w):
    a = client.post("/api/appointments", json=booking(w.doctor, w.patient, msk(13, 10)), headers=auth(w.admin))
    b = client.post(
        "/api/appointments", json=booking(w.doctor, w.other_patient, msk(13, 10, 30)), headers=auth(w.admin)
    )
    ra, rb = await asyncio.gather(a, b)
    assert (ra.status_code, rb.status_code) == (201, 201)
    assert len(await db_rows()) == 2


async def test_concurrent_same_patient_two_doctors_same_time_only_one_wins(client, auth, w):
    a = client.post("/api/appointments", json=booking(w.doctor, w.patient, msk(13, 10)), headers=auth(w.admin))
    b = client.post("/api/appointments", json=booking(w.other_doctor, w.patient, msk(13, 10)), headers=auth(w.admin))
    ra, rb = await asyncio.gather(a, b)
    assert sorted([ra.status_code, rb.status_code]) == [201, 409]
    assert len(await db_rows()) == 1


async def test_concurrent_cancel_twice_one_succeeds_other_conflicts(client, auth, session, w):
    appt = await add_appt(session, w.doctor, w.patient, msk(13, 10), creator=w.admin)
    r1, r2 = await asyncio.gather(
        client.post(f"/api/appointments/{appt.id}/cancel", headers=auth(w.admin)),
        client.post(f"/api/appointments/{appt.id}/cancel", headers=auth(w.registrar)),
    )
    assert sorted([r1.status_code, r2.status_code]) == [200, 409]
    loser = r1 if r1.status_code == 409 else r2
    assert loser.json() == {"detail": "Cannot change status from 'cancelled' to 'cancelled'"}
    assert (await db_appt(appt.id)).status == CANCELLED


async def test_concurrent_complete_and_no_show_exactly_one_wins(client, auth, session, w):
    appt = await add_appt(session, w.doctor, w.patient, msk(13, 10), creator=w.admin)
    set_now(msk(13, 10))
    r_c, r_n = await asyncio.gather(
        client.post(f"/api/appointments/{appt.id}/complete", headers=auth(w.doctor_user)),
        client.post(f"/api/appointments/{appt.id}/no-show", headers=auth(w.doctor_user)),
    )
    assert sorted([r_c.status_code, r_n.status_code]) == [200, 409]
    winner = COMPLETED if r_c.status_code == 200 else NO_SHOW
    assert (await db_appt(appt.id)).status == winner


async def test_concurrent_cancel_and_complete_service_exactly_one_wins(session, make_user, w):
    """Разные «часы» у двух конкурентов: отмена до начала и завершение после начала."""
    appt = await add_appt(session, w.doctor, w.patient, msk(13, 10), creator=w.admin)
    async with db.SessionFactory() as s1, db.SessionFactory() as s2:
        u1 = await s1.get(User, w.admin.id)
        u2 = await s2.get(User, w.doctor_user.id)
        results = await asyncio.gather(
            svc.change_status(s1, u1, appt.id, CANCELLED, now=msk(13, 9)),
            svc.change_status(s2, u2, appt.id, COMPLETED, now=msk(13, 10, 5)),
            return_exceptions=True,
        )
    ok = [r for r in results if isinstance(r, Appointment)]
    errors = [r for r in results if isinstance(r, Exception)]
    assert len(ok) == 1 and len(errors) == 1
    assert isinstance(errors[0], svc.ConflictError)
    assert (await db_appt(appt.id)).status == ok[0].status


async def test_concurrent_reschedules_into_same_slot_only_one_wins(client, auth, session, w):
    a = await add_appt(session, w.doctor, w.patient, msk(13, 10), creator=w.admin)
    b = await add_appt(session, w.doctor, w.other_patient, msk(13, 11), creator=w.admin)
    ra, rb = await asyncio.gather(
        client.patch(f"/api/appointments/{a.id}", json={"starts_at": msk(14, 10).isoformat()}, headers=auth(w.admin)),
        client.patch(f"/api/appointments/{b.id}", json={"starts_at": msk(14, 10).isoformat()}, headers=auth(w.admin)),
    )
    assert sorted([ra.status_code, rb.status_code]) == [200, 409]
    moved = [x for x in await db_rows() if x.starts_at == msk(14, 10)]
    assert len(moved) == 1


async def test_concurrent_cancel_and_reschedule_one_consistent_final_state(client, auth, session, w):
    appt = await add_appt(session, w.doctor, w.patient, msk(13, 10), creator=w.admin)
    rc, rp = await asyncio.gather(
        client.post(f"/api/appointments/{appt.id}/cancel", headers=auth(w.admin)),
        client.patch(
            f"/api/appointments/{appt.id}", json={"starts_at": msk(14, 10).isoformat()}, headers=auth(w.admin)
        ),
    )
    final = await db_appt(appt.id)
    assert rc.status_code == 200 and final.status == CANCELLED
    if rp.status_code == 200:  # перенос выполнился раньше отмены
        assert final.starts_at == msk(14, 10)
    else:
        assert rp.status_code == 409 and final.starts_at == msk(13, 10)


# --------------------------------------------------------------------------- #
# Целостность и транзакции
# --------------------------------------------------------------------------- #


def _integrity(message: str) -> IntegrityError:
    return IntegrityError("stmt", {}, Exception(message))


def _patch_commit(monkeypatch, message: str) -> None:
    async def failing(self):
        raise _integrity(message)

    monkeypatch.setattr(AsyncSession, "commit", failing)


async def test_create_unique_integrity_error_on_commit_returns_409_and_no_row(client, auth, w, monkeypatch):
    _patch_commit(monkeypatch, "UNIQUE constraint failed: appointments.doctor_id, appointments.starts_at")
    r = await client.post("/api/appointments", json=booking(w.doctor, w.patient, msk(13, 10)), headers=auth(w.admin))
    assert r.status_code == 409
    assert r.json() == {"detail": "This time slot has just been taken; please choose another one"}
    monkeypatch.undo()
    assert await db_rows() == []


async def test_create_foreign_key_integrity_error_is_not_409(client, auth, w, monkeypatch):
    _patch_commit(monkeypatch, "FOREIGN KEY constraint failed")
    r = await client.post("/api/appointments", json=booking(w.doctor, w.patient, msk(13, 10)), headers=auth(w.admin))
    assert r.status_code == 500
    monkeypatch.undo()
    assert await db_rows() == []


async def test_create_service_reraises_non_unique_integrity_error(session, w, monkeypatch):
    data = AppointmentCreate(
        doctor_id=w.doctor.id, patient_id=w.patient.id, starts_at=msk(13, 10), ends_at=msk(13, 10, 30)
    )
    _patch_commit(monkeypatch, "CHECK constraint failed: ends_after_starts")
    with pytest.raises(IntegrityError):
        await svc.create_appointment(session, w.admin, data, now=FROZEN)
    monkeypatch.undo()
    assert await db_rows() == []


async def test_create_service_converts_unique_integrity_error_to_conflict(session, w, monkeypatch):
    data = AppointmentCreate(
        doctor_id=w.doctor.id, patient_id=w.patient.id, starts_at=msk(13, 10), ends_at=msk(13, 10, 30)
    )
    _patch_commit(monkeypatch, "UNIQUE constraint failed: x")
    with pytest.raises(svc.ConflictError) as exc:
        await svc.create_appointment(session, w.admin, data, now=FROZEN)
    assert exc.value.detail == "This time slot has just been taken; please choose another one"


async def test_reschedule_failed_commit_keeps_old_interval(client, auth, session, w, monkeypatch):
    appt = await add_appt(session, w.doctor, w.patient, msk(13, 10), creator=w.admin)
    _patch_commit(monkeypatch, "FOREIGN KEY constraint failed")
    r = await client.patch(
        f"/api/appointments/{appt.id}",
        json={"starts_at": msk(14, 10).isoformat(), "reason": "новая"},
        headers=auth(w.admin),
    )
    assert r.status_code == 500
    monkeypatch.undo()
    row = await db_appt(appt.id)
    assert (row.starts_at, row.ends_at, row.reason) == (msk(13, 10), msk(13, 10, 30), None)


async def test_status_change_unique_error_returns_409_and_status_unchanged(client, auth, session, w, monkeypatch):
    appt = await add_appt(session, w.doctor, w.patient, msk(13, 10), creator=w.admin)
    _patch_commit(monkeypatch, "UNIQUE constraint failed: x")
    r = await client.post(f"/api/appointments/{appt.id}/cancel", headers=auth(w.admin))
    assert r.status_code == 409
    assert r.json() == {"detail": "The appointment was modified concurrently; please retry"}
    monkeypatch.undo()
    assert (await db_appt(appt.id)).status == SCHEDULED


async def test_status_change_other_integrity_error_propagates(client, auth, session, w, monkeypatch):
    appt = await add_appt(session, w.doctor, w.patient, msk(13, 10), creator=w.admin)
    _patch_commit(monkeypatch, "CHECK constraint failed")
    r = await client.post(f"/api/appointments/{appt.id}/cancel", headers=auth(w.admin))
    assert r.status_code == 500
    monkeypatch.undo()
    assert (await db_appt(appt.id)).status == SCHEDULED


async def test_partial_unique_index_race_on_create_returns_409(client, auth, w, monkeypatch):
    """Проверка пересечений «слепа» (гонка): последний рубеж - настоящий уникальный индекс."""

    async def blind(*args, **kwargs):
        return False

    monkeypatch.setattr(svc, "_has_overlap", blind)
    h = auth(w.admin)
    assert (
        await client.post("/api/appointments", json=booking(w.doctor, w.patient, msk(13, 10)), headers=h)
    ).status_code == 201
    r = await client.post("/api/appointments", json=booking(w.doctor, w.other_patient, msk(13, 10)), headers=h)
    assert r.status_code == 409
    assert r.json() == {"detail": "This time slot has just been taken; please choose another one"}
    assert len(await db_rows()) == 1


async def test_partial_unique_index_race_on_reschedule_returns_409_and_keeps_row(client, auth, session, w, monkeypatch):
    async def blind(*args, **kwargs):
        return False

    await add_appt(session, w.doctor, w.patient, msk(13, 10), creator=w.admin)
    b = await add_appt(session, w.doctor, w.other_patient, msk(13, 11), creator=w.admin)
    monkeypatch.setattr(svc, "_has_overlap", blind)
    r = await client.patch(
        f"/api/appointments/{b.id}", json={"starts_at": msk(13, 10).isoformat()}, headers=auth(w.admin)
    )
    assert r.status_code == 409
    assert r.json() == {"detail": "This time slot has just been taken; please choose another one"}
    assert (await db_appt(b.id)).starts_at == msk(13, 11)


async def test_index_allows_cancelled_duplicate_but_rejects_active_duplicate(session, w):
    await add_appt(session, w.doctor, w.patient, msk(13, 10), creator=w.admin)
    await add_appt(session, w.doctor, w.other_patient, msk(13, 10), status=CANCELLED, creator=w.admin)
    with pytest.raises(IntegrityError):
        await add_appt(session, w.doctor, w.other_patient, msk(13, 10), creator=w.admin)
    await session.rollback()
    async with db.SessionFactory() as s:
        assert await s.scalar(select(func.count()).select_from(Appointment)) == 2


# --------------------------------------------------------------------------- #
# Чтение одной записи и список
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("role", "code"),
    [
        ("admin", 200),
        ("registrar", 200),
        ("doctor_user", 200),
        ("patient_user", 200),
        ("other_doctor_user", 404),
        ("other_patient_user", 404),
    ],
)
async def test_get_appointment_visibility_matrix(client, auth, session, w, role, code):
    appt = await add_appt(session, w.doctor, w.patient, msk(13, 10), creator=w.admin, reason="r")
    r = await client.get(f"/api/appointments/{appt.id}", headers=auth(actor(w, role)))
    assert r.status_code == code
    if code == 200:
        assert r.json()["id"] == appt.id and r.json()["reason"] == "r"
    else:
        assert r.json() == {"detail": "Appointment not found"}


async def test_get_appointment_errors(client, auth, w):
    assert (await client.get("/api/appointments/1")).status_code == 401
    r = await client.get("/api/appointments/99999", headers=auth(w.admin))
    assert r.status_code == 404 and r.json() == {"detail": "Appointment not found"}
    assert (await client.get("/api/appointments/0", headers=auth(w.admin))).status_code == 422
    assert (await client.get("/api/appointments/2147483648", headers=auth(w.admin))).status_code == 422


async def test_get_appointment_profileless_patient_and_doctor_get_404(client, auth, session, make_user, w):
    appt = await add_appt(session, w.doctor, w.patient, msk(13, 10), creator=w.admin)
    for role in (UserRole.PATIENT, UserRole.DOCTOR):
        user = await make_user(role)
        assert (await client.get(f"/api/appointments/{appt.id}", headers=auth(user))).status_code == 404


async def _ids(client, headers, **params):
    r = await client.get("/api/appointments", params=params, headers=headers)
    assert r.status_code == 200, r.text
    return [x["id"] for x in r.json()]


async def test_list_visibility_per_role(client, auth, session, make_user, w):
    a = await add_appt(session, w.doctor, w.patient, msk(13, 10), creator=w.admin)
    b = await add_appt(session, w.other_doctor, w.other_patient, msk(13, 11), creator=w.admin)
    c = await add_appt(session, w.doctor, w.other_patient, msk(13, 12), creator=w.admin)
    assert await _ids(client, auth(w.admin)) == [a.id, b.id, c.id]
    assert await _ids(client, auth(w.registrar)) == [a.id, b.id, c.id]
    assert await _ids(client, auth(w.doctor_user)) == [a.id, c.id]
    assert await _ids(client, auth(w.patient_user)) == [a.id]
    assert await _ids(client, auth(w.other_patient_user)) == [b.id, c.id]
    assert await _ids(client, auth(await make_user(UserRole.PATIENT))) == []
    assert await _ids(client, auth(await make_user(UserRole.DOCTOR))) == []


async def test_list_requires_auth(client):
    assert (await client.get("/api/appointments")).status_code == 401


async def test_list_filters_combine_with_visibility(client, auth, session, w):
    a = await add_appt(session, w.doctor, w.patient, msk(13, 10), creator=w.admin)
    b = await add_appt(session, w.other_doctor, w.patient, msk(13, 11), status=COMPLETED, creator=w.admin)
    c = await add_appt(session, w.doctor, w.other_patient, msk(13, 12), status=CANCELLED, creator=w.admin)
    h = auth(w.admin)
    assert await _ids(client, h, doctor_id=w.doctor.id) == [a.id, c.id]
    assert await _ids(client, h, patient_id=w.patient.id) == [a.id, b.id]
    assert await _ids(client, h, status="completed") == [b.id]
    assert await _ids(client, h, status="cancelled", doctor_id=w.doctor.id) == [c.id]
    assert await _ids(client, h, doctor_id=99999) == []
    assert await _ids(client, auth(w.doctor_user), doctor_id=w.other_doctor.id) == []
    assert await _ids(client, auth(w.patient_user), patient_id=w.other_patient.id) == []


async def test_list_date_filters_use_clinic_local_days_inclusive(client, auth, session, w):
    night = await add_appt(session, w.doctor, w.patient, msk(14, 1), creator=w.admin)  # 22:00 UTC 13.10
    late = await add_appt(session, w.doctor, w.other_patient, msk(13, 23, 30), creator=w.admin)  # 20:30 UTC 13.10
    h = auth(w.admin)
    assert await _ids(client, h, date_from="2026-10-14") == [night.id]
    assert await _ids(client, h, date_to="2026-10-13") == [late.id]
    assert await _ids(client, h, date_from="2026-10-13", date_to="2026-10-13") == [late.id]
    assert await _ids(client, h, date_from="2026-10-14", date_to="2026-10-14") == [night.id]
    assert await _ids(client, h, date_from="2026-10-13", date_to="2026-10-14") == [late.id, night.id]
    assert await _ids(client, h, date_from="2026-10-15") == []


async def test_list_date_from_after_date_to_returns_422(client, auth, w):
    r = await client.get(
        "/api/appointments", params={"date_from": "2026-10-14", "date_to": "2026-10-13"}, headers=auth(w.admin)
    )
    assert r.status_code == 422
    assert r.json() == {"detail": "date_from must not be later than date_to"}


@pytest.mark.parametrize(
    "params",
    [
        pytest.param({"limit": 0}, id="limit_0"),
        pytest.param({"limit": 101}, id="limit_101"),
        pytest.param({"offset": -1}, id="offset_negative"),
        pytest.param({"status": "unknown"}, id="bad_status"),
        pytest.param({"doctor_id": 0}, id="doctor_id_0"),
        pytest.param({"patient_id": 2147483648}, id="patient_id_over"),
        pytest.param({"date_from": "1969-12-31"}, id="date_too_early"),
        pytest.param({"date_to": "2101-01-01"}, id="date_too_late"),
        pytest.param({"date_from": "abc"}, id="date_garbage"),
    ],
)
async def test_list_invalid_params_return_422(client, auth, w, params):
    r = await client.get("/api/appointments", params=params, headers=auth(w.admin))
    assert r.status_code == 422


async def test_list_pagination_and_stable_order(client, auth, session, w):
    ids = []
    ids.append((await add_appt(session, w.doctor, w.patient, msk(13, 12), creator=w.admin)).id)
    ids.append((await add_appt(session, w.doctor, w.patient, msk(13, 10), creator=w.admin)).id)
    tie1 = await add_appt(session, w.doctor, w.other_patient, msk(13, 11), creator=w.admin)
    tie2 = await add_appt(session, w.other_doctor, w.patient, msk(13, 11), creator=w.admin)
    expected = [ids[1], tie1.id, tie2.id, ids[0]]
    h = auth(w.admin)
    assert await _ids(client, h) == expected
    assert await _ids(client, h, limit=2) == expected[:2]
    assert await _ids(client, h, limit=2, offset=2) == expected[2:]
    assert await _ids(client, h, limit=1, offset=3) == expected[3:]
    assert await _ids(client, h, offset=4) == []
    assert await _ids(client, h, limit=100) == expected


async def test_list_default_limit_is_20(client, auth, session, w):
    for i in range(21):
        await add_appt(
            session, w.doctor, await add_patient(session), msk(13, 9) + timedelta(minutes=i), minutes=1, creator=w.admin
        )
    assert len(await _ids(client, auth(w.admin))) == 20
    assert len(await _ids(client, auth(w.admin), offset=20)) == 1


# --------------------------------------------------------------------------- #
# Статусы: cancel / complete / no-show
# --------------------------------------------------------------------------- #

CANCEL_MATRIX = [
    ("admin", 200),
    ("registrar", 200),
    ("patient_user", 200),
    ("other_patient_user", 404),
    ("doctor_user", 403),
]


@pytest.mark.parametrize(("role", "code"), CANCEL_MATRIX)
async def test_cancel_role_matrix(client, auth, session, w, role, code):
    appt = await add_appt(session, w.doctor, w.patient, msk(13, 10), creator=w.admin)
    r = await client.post(f"/api/appointments/{appt.id}/cancel", headers=auth(actor(w, role)))
    assert r.status_code == code
    if code == 200:
        assert r.json()["status"] == "cancelled" and r.json()["id"] == appt.id
        assert (await db_appt(appt.id)).status == CANCELLED
    else:
        assert (await db_appt(appt.id)).status == SCHEDULED
    if code == 403:
        assert r.json() == {"detail": "Insufficient permissions"}
    if code == 404:
        assert r.json() == {"detail": "Appointment not found"}


@pytest.mark.parametrize(
    ("role", "code"),
    [("doctor_user", 200), ("other_doctor_user", 404), ("admin", 403), ("registrar", 403), ("patient_user", 403)],
)
@pytest.mark.parametrize(("path", "final"), [("complete", COMPLETED), ("no-show", NO_SHOW)])
async def test_complete_and_no_show_role_matrix(client, auth, session, w, role, code, path, final):
    appt = await add_appt(session, w.doctor, w.patient, msk(13, 10), creator=w.admin)
    set_now(msk(13, 10, 5))
    r = await client.post(f"/api/appointments/{appt.id}/{path}", headers=auth(actor(w, role)))
    assert r.status_code == code, r.text
    assert (await db_appt(appt.id)).status == (final if code == 200 else SCHEDULED)
    if code == 200:
        assert r.json()["status"] == final.value


@pytest.mark.parametrize("path", ["cancel", "complete", "no-show"])
async def test_status_endpoints_without_token_and_bad_ids(client, auth, w, path):
    assert (await client.post(f"/api/appointments/1/{path}")).status_code == 401
    user = w.doctor_user if path != "cancel" else w.admin
    r = await client.post(f"/api/appointments/99999/{path}", headers=auth(user))
    assert r.status_code == 404 and r.json() == {"detail": "Appointment not found"}
    assert (await client.post(f"/api/appointments/0/{path}", headers=auth(user))).status_code == 422


@pytest.mark.parametrize(
    ("delta", "code"),
    [
        pytest.param(timedelta(0), 200, id="exactly_at_start"),
        pytest.param(timedelta(seconds=-1), 422, id="one_second_before_start"),
        pytest.param(timedelta(seconds=1), 200, id="one_second_after_start"),
    ],
)
@pytest.mark.parametrize("path", ["complete", "no-show"])
async def test_complete_time_boundary(client, auth, session, w, path, delta, code):
    appt = await add_appt(session, w.doctor, w.patient, msk(13, 10), creator=w.admin)
    set_now(appt.starts_at + delta)
    r = await client.post(f"/api/appointments/{appt.id}/{path}", headers=auth(w.doctor_user))
    assert r.status_code == code
    if code == 422:
        assert r.json() == {"detail": "The appointment has not started yet"}
        assert (await db_appt(appt.id)).status == SCHEDULED


@pytest.mark.parametrize(
    ("delta", "code"),
    [
        pytest.param(timedelta(seconds=-1), 200, id="one_second_before_start"),
        pytest.param(timedelta(0), 422, id="exactly_at_start"),
        pytest.param(timedelta(minutes=5), 422, id="after_start"),
    ],
)
@pytest.mark.parametrize("role", ["admin", "registrar"])
async def test_cancel_started_appointment_rule(client, auth, session, w, role, delta, code):
    appt = await add_appt(session, w.doctor, w.patient, msk(13, 10), creator=w.admin)
    set_now(appt.starts_at + delta)
    r = await client.post(f"/api/appointments/{appt.id}/cancel", headers=auth(actor(w, role)))
    assert r.status_code == code
    if code == 422:
        assert "already started" in r.json()["detail"]
        assert (await db_appt(appt.id)).status == SCHEDULED


@pytest.mark.parametrize(
    ("before", "code"),
    [
        pytest.param(timedelta(hours=2, seconds=1), 200, id="2h_plus_1s"),
        pytest.param(timedelta(hours=2), 200, id="exactly_2h"),
        pytest.param(timedelta(hours=2) - timedelta(seconds=1), 403, id="2h_minus_1s"),
        pytest.param(timedelta(minutes=1), 403, id="1_minute"),
    ],
)
async def test_patient_cancel_two_hour_window(client, auth, session, w, before, code):
    appt = await add_appt(session, w.doctor, w.patient, msk(13, 10), creator=w.admin)
    set_now(appt.starts_at - before)
    r = await client.post(f"/api/appointments/{appt.id}/cancel", headers=auth(w.patient_user))
    assert r.status_code == code
    if code == 403:
        assert "at least 2 hours" in r.json()["detail"]
        assert (await db_appt(appt.id)).status == SCHEDULED
    else:
        assert (await db_appt(appt.id)).status == CANCELLED


async def test_staff_cancel_inside_two_hours_allowed(client, auth, session, w):
    appt = await add_appt(session, w.doctor, w.patient, msk(13, 10), creator=w.admin)
    set_now(appt.starts_at - timedelta(minutes=1))
    r = await client.post(f"/api/appointments/{appt.id}/cancel", headers=auth(w.registrar))
    assert r.status_code == 200


@pytest.mark.parametrize("start_status", [COMPLETED, NO_SHOW, CANCELLED])
@pytest.mark.parametrize(
    ("path", "user", "target"),
    [("cancel", "admin", CANCELLED), ("complete", "doctor_user", COMPLETED), ("no-show", "doctor_user", NO_SHOW)],
)
async def test_terminal_statuses_have_no_exit(client, auth, session, w, start_status, path, user, target):
    appt = await add_appt(session, w.doctor, w.patient, msk(13, 10), status=start_status, creator=w.admin)
    set_now(msk(13, 9))
    r = await client.post(f"/api/appointments/{appt.id}/{path}", headers=auth(actor(w, user)))
    assert r.status_code == 409
    assert r.json() == {"detail": f"Cannot change status from '{start_status.value}' to '{target.value}'"}
    assert (await db_appt(appt.id)).status == start_status


# --------------------------------------------------------------------------- #
# PATCH
# --------------------------------------------------------------------------- #


async def _patch(client, auth, user, appt_id, body):
    return await client.patch(f"/api/appointments/{appt_id}", json=body, headers=auth(user))


@pytest.mark.parametrize("status", ["cancelled", "completed", "scheduled", "no_show"])
async def test_patch_status_is_forbidden(client, auth, session, w, status):
    appt = await add_appt(session, w.doctor, w.patient, msk(13, 10), creator=w.admin)
    r = await _patch(client, auth, w.admin, appt.id, {"status": status})
    assert r.status_code == 422
    assert r.json() == {"detail": "Status cannot be changed via PATCH; use /cancel, /complete or /no-show"}
    assert (await db_appt(appt.id)).status == SCHEDULED


async def test_patch_status_together_with_reason_changes_nothing(client, auth, session, w):
    appt = await add_appt(session, w.doctor, w.patient, msk(13, 10), creator=w.admin)
    r = await _patch(client, auth, w.admin, appt.id, {"status": "cancelled", "reason": "x"})
    assert r.status_code == 422
    assert (await db_appt(appt.id)).reason is None


async def test_patch_empty_body_returns_422(client, auth, session, w):
    appt = await add_appt(session, w.doctor, w.patient, msk(13, 10), creator=w.admin)
    r = await _patch(client, auth, w.admin, appt.id, {})
    assert r.status_code == 422 and r.json() == {"detail": "No fields to update"}


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"starts_at": None}, id="null_starts_at"),
        pytest.param({"ends_at": None}, id="null_ends_at"),
        pytest.param({"doctor_id": 2}, id="extra_doctor_id"),
        pytest.param({"reason": ""}, id="empty_reason"),
        pytest.param({"starts_at": "2026-10-13T10:00:00"}, id="naive"),
    ],
)
async def test_patch_invalid_body_returns_422(client, auth, session, w, body):
    appt = await add_appt(session, w.doctor, w.patient, msk(13, 10), creator=w.admin)
    r = await _patch(client, auth, w.admin, appt.id, body)
    assert r.status_code == 422 and isinstance(r.json()["detail"], list)


async def test_patch_role_matrix(client, auth, session, w):
    appt = await add_appt(session, w.doctor, w.patient, msk(14, 10), creator=w.admin)
    for role, code in [
        ("admin", 200),
        ("registrar", 200),
        ("patient_user", 200),
        ("doctor_user", 403),
        ("other_patient_user", 404),
    ]:
        r = await _patch(client, auth, actor(w, role), appt.id, {"reason": f"by {role}"})
        assert r.status_code == code, (role, r.text)
        if code == 200:
            assert (await db_appt(appt.id)).reason == f"by {role}"
    assert (await client.patch(f"/api/appointments/{appt.id}", json={"reason": "x"})).status_code == 401
    r = await _patch(client, auth, w.admin, 99999, {"reason": "x"})
    assert r.status_code == 404


async def test_patch_start_only_keeps_duration(client, auth, session, w):
    appt = await add_appt(session, w.doctor, w.patient, msk(13, 10), minutes=90, creator=w.admin)
    r = await _patch(client, auth, w.admin, appt.id, {"starts_at": msk(14, 14).isoformat()})
    assert r.status_code == 200
    row = await db_appt(appt.id)
    assert (row.starts_at, row.ends_at) == (msk(14, 14), msk(14, 15, 30))


async def test_patch_end_only_changes_duration(client, auth, session, w):
    appt = await add_appt(session, w.doctor, w.patient, msk(13, 10), creator=w.admin)
    r = await _patch(client, auth, w.admin, appt.id, {"ends_at": msk(13, 11).isoformat()})
    assert r.status_code == 200
    row = await db_appt(appt.id)
    assert (row.starts_at, row.ends_at) == (msk(13, 10), msk(13, 11))


async def test_patch_end_not_after_existing_start_returns_422(client, auth, session, w):
    appt = await add_appt(session, w.doctor, w.patient, msk(13, 10), creator=w.admin)
    r = await _patch(client, auth, w.admin, appt.id, {"ends_at": msk(13, 10).isoformat()})
    assert r.status_code == 422 and r.json() == {"detail": "ends_at must be later than starts_at"}


async def test_patch_both_bounds_moves_interval(client, auth, session, w):
    appt = await add_appt(session, w.doctor, w.patient, msk(13, 10), creator=w.admin)
    r = await _patch(
        client, auth, w.admin, appt.id, {"starts_at": msk(15, 9).isoformat(), "ends_at": msk(15, 10).isoformat()}
    )
    assert r.status_code == 200
    assert r.json()["status"] == "scheduled"
    row = await db_appt(appt.id)
    assert (row.starts_at, row.ends_at) == (msk(15, 9), msk(15, 10))


async def test_patch_shift_inside_own_interval_does_not_conflict_with_itself(client, auth, session, w):
    appt = await add_appt(session, w.doctor, w.patient, msk(13, 10), minutes=60, creator=w.admin)
    r = await _patch(client, auth, w.admin, appt.id, {"starts_at": msk(13, 10, 30).isoformat()})
    assert r.status_code == 200
    assert (await db_appt(appt.id)).starts_at == msk(13, 10, 30)


async def test_patch_into_other_doctor_slot_returns_409(client, auth, session, w):
    await add_appt(session, w.doctor, w.other_patient, msk(14, 10), creator=w.admin)
    appt = await add_appt(session, w.doctor, w.patient, msk(13, 10), creator=w.admin)
    r = await _patch(client, auth, w.admin, appt.id, {"starts_at": msk(14, 10).isoformat()})
    assert r.status_code == 409
    assert r.json() == {"detail": "The doctor already has an appointment at this time"}
    assert (await db_appt(appt.id)).starts_at == msk(13, 10)


async def test_patch_into_patients_other_appointment_returns_409(client, auth, session, w):
    await add_appt(session, w.other_doctor, w.patient, msk(14, 10), creator=w.admin)
    appt = await add_appt(session, w.doctor, w.patient, msk(13, 10), creator=w.admin)
    r = await _patch(client, auth, w.admin, appt.id, {"starts_at": msk(14, 10).isoformat()})
    assert r.status_code == 409
    assert r.json() == {"detail": "The patient already has an appointment at this time"}


@pytest.mark.parametrize(
    ("start", "fragment"),
    [
        pytest.param(msk(13, 8, 30), "shift", id="before_shift"),
        pytest.param(msk(13, 9, 15), "slot boundary", id="off_grid"),
        pytest.param(msk(17, 10), "day of the week", id="saturday"),
        pytest.param(msk(12, 9), "future", id="now"),
        pytest.param(FROZEN + timedelta(days=90, minutes=30), "90 days", id="beyond_90_days"),
    ],
)
async def test_patch_move_validates_rules(client, auth, session, w, start, fragment):
    appt = await add_appt(session, w.doctor, w.patient, msk(14, 10), creator=w.admin)
    r = await _patch(client, auth, w.admin, appt.id, {"starts_at": start.isoformat()})
    assert r.status_code == 422, r.text
    assert fragment in r.json()["detail"]
    assert (await db_appt(appt.id)).starts_at == msk(14, 10)


async def test_patch_move_overflow_returns_422(client, auth, session, w):
    appt = await add_appt(session, w.doctor, w.patient, msk(14, 10), creator=w.admin)
    r = await _patch(client, auth, w.admin, appt.id, {"starts_at": "9999-12-31T23:30:00Z"})
    assert r.status_code == 422
    assert r.json() == {"detail": "starts_at is out of the supported range"}


async def test_patch_move_with_deactivated_doctor_returns_422_but_reason_still_ok(client, auth, session, w):
    appt = await add_appt(session, w.doctor, w.patient, msk(14, 10), creator=w.admin)
    w.doctor_user.is_active = False
    await session.commit()
    r = await _patch(client, auth, w.admin, appt.id, {"starts_at": msk(15, 10).isoformat()})
    assert r.status_code == 422 and r.json() == {"detail": "The doctor is not available for new appointments"}
    r = await _patch(client, auth, w.admin, appt.id, {"reason": "ok"})
    assert r.status_code == 200 and (await db_appt(appt.id)).reason == "ok"


@pytest.mark.parametrize("status", [COMPLETED, NO_SHOW, CANCELLED])
async def test_patch_non_scheduled_returns_409(client, auth, session, w, status):
    appt = await add_appt(session, w.doctor, w.patient, msk(14, 10), status=status, creator=w.admin)
    r = await _patch(client, auth, w.admin, appt.id, {"reason": "x"})
    assert r.status_code == 409 and r.json() == {"detail": "Only scheduled appointments can be modified"}


async def test_patch_reason_set_and_cleared_does_not_touch_interval(client, auth, session, w):
    appt = await add_appt(session, w.doctor, w.patient, msk(12, 8), creator=w.admin, reason="old")  # уже в прошлом
    r = await _patch(client, auth, w.admin, appt.id, {"reason": None})
    assert r.status_code == 200 and r.json()["reason"] is None
    row = await db_appt(appt.id)
    assert (row.reason, row.starts_at) == (None, msk(12, 8))


@pytest.mark.parametrize(
    ("before", "code"),
    [
        pytest.param(timedelta(hours=2), 200, id="patient_exactly_2h"),
        pytest.param(timedelta(hours=2, seconds=1), 200, id="patient_2h_plus_1s"),
        pytest.param(timedelta(hours=2) - timedelta(seconds=1), 403, id="patient_2h_minus_1s"),
    ],
)
async def test_patch_patient_two_hour_window(client, auth, session, w, before, code):
    appt = await add_appt(session, w.doctor, w.patient, msk(14, 10), creator=w.admin)
    set_now(appt.starts_at - before)
    r = await _patch(client, auth, w.patient_user, appt.id, {"starts_at": msk(15, 10).isoformat()})
    assert r.status_code == code, r.text
    row = await db_appt(appt.id)
    if code == 403:
        assert "at least 2 hours" in r.json()["detail"] and row.starts_at == msk(14, 10)
    else:
        assert row.starts_at == msk(15, 10)


async def test_patch_staff_can_move_inside_two_hours(client, auth, session, w):
    appt = await add_appt(session, w.doctor, w.patient, msk(14, 10), creator=w.admin)
    set_now(appt.starts_at - timedelta(minutes=10))
    r = await _patch(client, auth, w.registrar, appt.id, {"starts_at": msk(15, 10).isoformat()})
    assert r.status_code == 200


# --------------------------------------------------------------------------- #
# Свободные слоты
# --------------------------------------------------------------------------- #


async def _slots(client, headers, doctor_id, day):
    r = await client.get(f"/api/doctors/{doctor_id}/slots", params={"date": day}, headers=headers)
    assert r.status_code == 200, r.text
    return [(datetime.fromisoformat(x["starts_at"]), datetime.fromisoformat(x["ends_at"])) for x in r.json()]


@pytest.mark.parametrize("role", ["admin", "registrar", "doctor_user", "patient_user", "other_doctor_user"])
async def test_slots_available_to_every_authenticated_role(client, auth, w, role):
    slots = await _slots(client, auth(actor(w, role)), w.doctor.id, "2026-10-13")
    assert len(slots) == 18
    assert slots[0] == (msk(13, 9), msk(13, 9, 30))
    assert slots[-1] == (msk(13, 17, 30), msk(13, 18))


async def test_slots_without_token_returns_401(client, w):
    r = await client.get(f"/api/doctors/{w.doctor.id}/slots", params={"date": "2026-10-13"})
    assert r.status_code == 401


async def test_slots_unknown_doctor_returns_404(client, auth, w):
    r = await client.get("/api/doctors/99999/slots", params={"date": "2026-10-13"}, headers=auth(w.admin))
    assert r.status_code == 404 and r.json() == {"detail": "Doctor not found"}


@pytest.mark.parametrize(
    "params",
    [{}, {"date": "bad"}, {"date": "1969-12-31"}, {"date": "2101-01-01"}],
    ids=["missing", "garbage", "too_early", "too_late"],
)
async def test_slots_invalid_date_returns_422(client, auth, w, params):
    r = await client.get(f"/api/doctors/{w.doctor.id}/slots", params=params, headers=auth(w.admin))
    assert r.status_code == 422


async def test_slots_doctor_id_zero_returns_422(client, auth, w):
    r = await client.get("/api/doctors/0/slots", params={"date": "2026-10-13"}, headers=auth(w.admin))
    assert r.status_code == 422


async def test_slots_today_exclude_past_and_current_slot(client, auth, w):
    slots = await _slots(client, auth(w.admin), w.doctor.id, "2026-10-12")
    assert slots[0][0] == msk(12, 9, 30)  # 09:00 == now: не предлагается
    assert len(slots) == 17


async def test_slots_after_clock_moves_hide_earlier_slots(client, auth, w):
    set_now(msk(12, 17, 29))
    slots = await _slots(client, auth(w.admin), w.doctor.id, "2026-10-12")
    assert slots == [(msk(12, 17, 30), msk(12, 18))]
    set_now(msk(12, 17, 30))
    assert await _slots(client, auth(w.admin), w.doctor.id, "2026-10-12") == []


async def test_slots_past_day_is_empty(client, auth, w):
    assert await _slots(client, auth(w.admin), w.doctor.id, "2026-10-05") == []


@pytest.mark.parametrize("day", ["2026-10-17", "2026-10-18"], ids=["saturday", "sunday"])
async def test_slots_non_working_day_is_empty(client, auth, w, day):
    assert await _slots(client, auth(w.admin), w.doctor.id, day) == []


async def test_slots_exclude_busy_multislot_but_not_cancelled(client, auth, session, w):
    await add_appt(session, w.doctor, w.patient, msk(13, 10), minutes=60, creator=w.admin)
    await add_appt(session, w.doctor, w.patient, msk(13, 14), status=CANCELLED, creator=w.admin)
    await add_appt(session, w.other_doctor, w.patient, msk(13, 15), creator=w.admin)  # чужой врач
    starts = [s for s, _ in await _slots(client, auth(w.admin), w.doctor.id, "2026-10-13")]
    assert msk(13, 10) not in starts and msk(13, 10, 30) not in starts
    assert msk(13, 9, 30) in starts and msk(13, 11) in starts
    assert msk(13, 14) in starts and msk(13, 15) in starts
    assert len(starts) == 16


async def test_slots_deactivated_doctor_returns_empty_list(client, auth, session, w):
    w.doctor_user.is_active = False
    await session.commit()
    assert await _slots(client, auth(w.admin), w.doctor.id, "2026-10-13") == []


async def test_slots_custom_slot_minutes(client, auth, make_doctor, w):
    d = await make_doctor(slot_minutes=60, work_start=time(9), work_end=time(12))
    slots = await _slots(client, auth(w.admin), d.id, "2026-10-13")
    assert slots == [(msk(13, 9), msk(13, 10)), (msk(13, 10), msk(13, 11)), (msk(13, 11), msk(13, 12))]


async def test_slots_ninety_day_limit_inclusive(client, auth, make_doctor, w):
    d = await make_doctor(work_days=[1, 2, 3, 4, 5, 6, 7])
    last_day = (FROZEN + timedelta(days=90)).astimezone(MSK).date().isoformat()
    slots = await _slots(client, auth(w.admin), d.id, last_day)
    assert slots == [(FROZEN + timedelta(days=90), FROZEN + timedelta(days=90, minutes=30))]
    next_day = (FROZEN + timedelta(days=91)).astimezone(MSK).date().isoformat()
    assert await _slots(client, auth(w.admin), d.id, next_day) == []


async def test_slot_from_list_can_be_booked_and_then_disappears(client, auth, w):
    h = auth(w.admin)
    start, end = (await _slots(client, h, w.doctor.id, "2026-10-13"))[3]
    body = {
        "doctor_id": w.doctor.id,
        "patient_id": w.patient.id,
        "starts_at": start.isoformat(),
        "ends_at": end.isoformat(),
    }
    assert (await client.post("/api/appointments", json=body, headers=h)).status_code == 201
    assert (start, end) not in await _slots(client, h, w.doctor.id, "2026-10-13")
    assert len(await _slots(client, h, w.doctor.id, "2026-10-13")) == 17

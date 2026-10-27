"""HTTP-тесты /api/doctors: чтение, создание профиля, смена графика при будущих записях."""

from datetime import UTC, date, datetime, time, timedelta

import pytest
from sqlalchemy import func, select

from app.models import Appointment, AppointmentStatus, Doctor, Patient, User, UserRole

URL = "/api/doctors"
FROZEN_NOW = datetime(2026, 10, 12, 6, 0, tzinfo=UTC)  # понедельник 09:00 МСК


def msk(day: int, hour: int, minute: int = 0) -> datetime:
    """Момент октября 2026 по Москве (UTC+3), как aware UTC."""
    return datetime(2026, 10, day, hour, minute, tzinfo=UTC) - timedelta(hours=3)


def doctor_body(user_id: int, **overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "user_id": user_id,
        "specialty": "Кардиолог",
        "cabinet": "205",
        "slot_minutes": 20,
        "work_start": "08:00:00",
        "work_end": "16:00:00",
        "work_days": [1, 3, 5],
    }
    body.update(overrides)
    return body


async def make_patient(session, policy: str = "1000000000000001") -> Patient:
    patient = Patient(full_name="Пациент Тестовый", birth_date=date(1990, 1, 1), policy_number=policy)
    session.add(patient)
    await session.commit()
    return patient


async def book(
    session, doctor, patient, creator, starts_at, minutes=30, status=AppointmentStatus.SCHEDULED, ends_at=None
):
    appointment = Appointment(
        doctor_id=doctor.id,
        patient_id=patient.id,
        starts_at=starts_at,
        ends_at=ends_at or starts_at + timedelta(minutes=minutes),
        status=status,
        created_by=creator.id,
    )
    session.add(appointment)
    await session.commit()
    return appointment


async def reload_doctor(session, doctor_id: int) -> Doctor:
    return await session.get(Doctor, doctor_id, populate_existing=True)


@pytest.fixture
async def admin(make_user):
    return await make_user(UserRole.ADMIN)


# --------------------------------------------------------------------------- #
# GET /api/doctors, GET /api/doctors/{id}
# --------------------------------------------------------------------------- #


async def test_get_doctor_returns_profile_with_full_name(client, make_doctor, make_user, auth, session):
    doctor = await make_doctor(specialty="Невролог", cabinet="12А", slot_minutes=15, work_days=[2, 4])
    viewer = await make_user(UserRole.PATIENT)
    owner = await session.get(User, doctor.user_id)

    response = await client.get(f"{URL}/{doctor.id}", headers=auth(viewer))

    assert response.status_code == 200
    assert response.json() == {
        "id": doctor.id,
        "user_id": doctor.user_id,
        "specialty": "Невролог",
        "cabinet": "12А",
        "slot_minutes": 15,
        "work_start": "09:00:00",
        "work_end": "18:00:00",
        "work_days": [2, 4],
        "full_name": owner.full_name,
    }


async def test_get_doctor_nonexistent_id_returns_404(client, make_user, auth):
    viewer = await make_user(UserRole.PATIENT)

    response = await client.get(f"{URL}/9999", headers=auth(viewer))

    assert response.status_code == 404
    assert response.json() == {"detail": "Doctor not found"}


@pytest.mark.parametrize("doctor_id", [0, -5, 2_147_483_648, "x"], ids=["zero", "negative", "over-int32", "text"])
async def test_get_doctor_invalid_id_returns_422(client, make_user, auth, doctor_id):
    viewer = await make_user(UserRole.PATIENT)

    response = await client.get(f"{URL}/{doctor_id}", headers=auth(viewer))

    assert response.status_code == 422


async def test_get_doctor_with_deactivated_account_is_still_readable_by_id(
    client, make_doctor, make_user, auth, session
):
    doctor = await make_doctor()
    (await session.get(User, doctor.user_id)).is_active = False
    await session.commit()
    viewer = await make_user(UserRole.PATIENT)

    response = await client.get(f"{URL}/{doctor.id}", headers=auth(viewer))

    assert response.status_code == 200
    assert response.json()["id"] == doctor.id


async def test_list_doctors_is_sorted_by_full_name_then_id(client, make_doctor, make_user, auth, session):
    d1, d2, d3 = await make_doctor(), await make_doctor(), await make_doctor()
    names = {d1: "Яковлев Яков", d2: "Андреев Андрей", d3: "Андреев Андрей"}
    for doctor, name in names.items():
        (await session.get(User, doctor.user_id)).full_name = name
    await session.commit()
    viewer = await make_user(UserRole.PATIENT)

    response = await client.get(URL, headers=auth(viewer))

    assert [d["id"] for d in response.json()] == [d2.id, d3.id, d1.id]


async def test_list_doctors_excludes_deactivated_accounts(client, make_doctor, make_user, auth, session):
    active = await make_doctor()
    inactive = await make_doctor()
    (await session.get(User, inactive.user_id)).is_active = False
    await session.commit()
    viewer = await make_user(UserRole.REGISTRAR)

    response = await client.get(URL, headers=auth(viewer))

    assert [d["id"] for d in response.json()] == [active.id]


async def test_list_doctors_empty_returns_empty_list(client, make_user, auth):
    viewer = await make_user(UserRole.PATIENT)

    response = await client.get(URL, headers=auth(viewer))

    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.parametrize(
    "query", ["терапевт", "ТЕРАПЕВТ", "Терапевт", "  терапевт  "], ids=["lower", "upper", "exact", "padded"]
)
async def test_list_doctors_specialty_filter_ignores_case_for_cyrillic(client, make_doctor, make_user, auth, query):
    therapist = await make_doctor(specialty="Терапевт")
    await make_doctor(specialty="Хирург")
    viewer = await make_user(UserRole.PATIENT)

    response = await client.get(URL, params={"specialty": query}, headers=auth(viewer))

    assert [d["id"] for d in response.json()] == [therapist.id]


async def test_list_doctors_specialty_filter_is_exact_not_substring(client, make_doctor, make_user, auth):
    await make_doctor(specialty="Терапевт")
    viewer = await make_user(UserRole.PATIENT)

    response = await client.get(URL, params={"specialty": "терап"}, headers=auth(viewer))

    assert response.json() == []


async def test_list_doctors_blank_specialty_returns_everyone(client, make_doctor, make_user, auth):
    await make_doctor(specialty="Терапевт")
    await make_doctor(specialty="Хирург")
    viewer = await make_user(UserRole.PATIENT)

    response = await client.get(URL, params={"specialty": "   "}, headers=auth(viewer))

    assert len(response.json()) == 2


@pytest.mark.parametrize(("length", "expected"), [(1, 200), (100, 200), (101, 422), (0, 422)])
async def test_list_doctors_specialty_length_boundaries(client, make_user, auth, length, expected):
    viewer = await make_user(UserRole.PATIENT)

    response = await client.get(URL, params={"specialty": "я" * length}, headers=auth(viewer))

    assert response.status_code == expected


# --------------------------------------------------------------------------- #
# POST /api/doctors
# --------------------------------------------------------------------------- #


async def test_create_doctor_stores_profile_and_returns_full_name(client, admin, make_user, auth, session):
    user = await make_user(UserRole.DOCTOR)

    response = await client.post(URL, json=doctor_body(user.id), headers=auth(admin))

    assert response.status_code == 201
    body = response.json()
    assert body == {
        "id": body["id"],
        "user_id": user.id,
        "specialty": "Кардиолог",
        "cabinet": "205",
        "slot_minutes": 20,
        "work_start": "08:00:00",
        "work_end": "16:00:00",
        "work_days": [1, 3, 5],
        "full_name": user.full_name,
    }
    stored = await reload_doctor(session, body["id"])
    assert (stored.user_id, stored.specialty, stored.cabinet, stored.slot_minutes) == (user.id, "Кардиолог", "205", 20)
    assert (stored.work_start, stored.work_end, stored.work_days) == (time(8), time(16), [1, 3, 5])


async def test_create_doctor_applies_defaults_for_slot_and_days(client, admin, make_user, auth):
    user = await make_user(UserRole.DOCTOR)
    body = doctor_body(user.id)
    del body["slot_minutes"], body["work_days"]

    response = await client.post(URL, json=body, headers=auth(admin))

    assert response.status_code == 201
    assert response.json()["slot_minutes"] == 30
    assert response.json()["work_days"] == [1, 2, 3, 4, 5]


async def test_create_doctor_sorts_work_days(client, admin, make_user, auth):
    user = await make_user(UserRole.DOCTOR)

    response = await client.post(URL, json=doctor_body(user.id, work_days=[5, 1, 3]), headers=auth(admin))

    assert response.json()["work_days"] == [1, 3, 5]


async def test_create_doctor_trims_text_fields(client, admin, make_user, auth):
    user = await make_user(UserRole.DOCTOR)

    response = await client.post(
        URL, json=doctor_body(user.id, specialty="  Лор  ", cabinet=" 7 "), headers=auth(admin)
    )

    assert (response.json()["specialty"], response.json()["cabinet"]) == ("Лор", "7")


async def test_create_doctor_for_nonexistent_user_returns_404(client, admin, auth, session):
    response = await client.post(URL, json=doctor_body(9999), headers=auth(admin))

    assert response.status_code == 404
    assert response.json() == {"detail": "User not found"}
    assert await session.scalar(select(func.count()).select_from(Doctor)) == 0


@pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.REGISTRAR, UserRole.PATIENT], ids=lambda r: r.value)
async def test_create_doctor_for_user_with_other_role_returns_422(client, admin, make_user, auth, session, role):
    user = await make_user(role)

    response = await client.post(URL, json=doctor_body(user.id), headers=auth(admin))

    assert response.status_code == 422
    assert response.json() == {"detail": "The user must have the 'doctor' role"}
    assert await session.scalar(select(func.count()).select_from(Doctor)) == 0


async def test_create_doctor_when_profile_exists_returns_409_and_keeps_old_profile(
    client, admin, make_doctor, auth, session
):
    existing = await make_doctor(specialty="Терапевт")

    response = await client.post(URL, json=doctor_body(existing.user_id), headers=auth(admin))

    assert response.status_code == 409
    assert response.json() == {"detail": "The user already has a doctor profile"}
    assert (await reload_doctor(session, existing.id)).specialty == "Терапевт"
    assert await session.scalar(select(func.count()).select_from(Doctor)) == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"work_start": "16:00:00"},
        {"work_start": "17:00:00"},
        {"work_start": "09:00:00+03:00"},
        {"work_end": "08:00:00"},
        {"work_start": "bad"},
        {"slot_minutes": 4},
        {"slot_minutes": 241},
        {"slot_minutes": 0},
        {"slot_minutes": "x"},
        {"work_days": []},
        {"work_days": [0]},
        {"work_days": [8]},
        {"work_days": [1, 1]},
        {"work_days": [1, 2, 3, 4, 5, 6, 7, 7]},
        {"work_days": "1"},
        {"specialty": "a"},
        {"specialty": "я" * 101},
        {"cabinet": ""},
        {"cabinet": "1" * 21},
        {"user_id": 0},
        {"user_id": 2_147_483_648},
        {"user_id": "abc"},
        {"is_active": True},
    ],
    ids=[
        "start-equals-end",
        "start-after-end",
        "start-with-offset",
        "end-before-start",
        "bad-time",
        "slot-4",
        "slot-241",
        "slot-0",
        "slot-text",
        "days-empty",
        "day-0",
        "day-8",
        "days-duplicate",
        "days-8-items",
        "days-not-list",
        "specialty-1",
        "specialty-101",
        "cabinet-empty",
        "cabinet-21",
        "user-0",
        "user-over-int32",
        "user-text",
        "extra-field",
    ],
)
async def test_create_doctor_invalid_body_returns_422(client, admin, make_user, auth, session, overrides):
    user = await make_user(UserRole.DOCTOR)
    body = doctor_body(user.id)
    body.update(overrides)

    response = await client.post(URL, json=body, headers=auth(admin))

    assert response.status_code == 422
    assert await session.scalar(select(func.count()).select_from(Doctor)) == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"slot_minutes": 5},
        {"slot_minutes": 240},
        {"work_days": [1, 2, 3, 4, 5, 6, 7]},
        {"work_days": [7]},
        {"specialty": "Яя"},
        {"specialty": "я" * 100},
        {"cabinet": "1"},
        {"cabinet": "1" * 20},
        {"work_start": "00:00:00", "work_end": "00:01:00"},
        {"work_start": "23:58:00", "work_end": "23:59:00"},
    ],
    ids=[
        "slot-5",
        "slot-240",
        "all-days",
        "sunday-only",
        "specialty-2",
        "specialty-100",
        "cabinet-1",
        "cabinet-20",
        "one-minute-at-midnight",
        "one-minute-before-midnight",
    ],
)
async def test_create_doctor_boundary_values_are_accepted(client, admin, make_user, auth, overrides):
    user = await make_user(UserRole.DOCTOR)

    response = await client.post(URL, json=doctor_body(user.id, **overrides), headers=auth(admin))

    assert response.status_code == 201


@pytest.mark.parametrize("field", ["user_id", "specialty", "cabinet", "work_start", "work_end"])
async def test_create_doctor_missing_required_field_returns_422(client, admin, make_user, auth, field):
    user = await make_user(UserRole.DOCTOR)
    body = doctor_body(user.id)
    del body[field]

    response = await client.post(URL, json=body, headers=auth(admin))

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", field]


# --------------------------------------------------------------------------- #
# PATCH /api/doctors/{id}
# --------------------------------------------------------------------------- #


async def test_update_doctor_changes_cabinet_and_specialty_only(client, admin, make_doctor, auth, session):
    doctor = await make_doctor()

    response = await client.patch(
        f"{URL}/{doctor.id}", json={"cabinet": "300", "specialty": "Кардиолог"}, headers=auth(admin)
    )

    assert response.status_code == 200
    assert (response.json()["cabinet"], response.json()["specialty"]) == ("300", "Кардиолог")
    stored = await reload_doctor(session, doctor.id)
    assert (stored.cabinet, stored.specialty) == ("300", "Кардиолог")
    assert (stored.slot_minutes, stored.work_start, stored.work_end, stored.work_days) == (
        30,
        time(9),
        time(18),
        [1, 2, 3, 4, 5],
    )


async def test_update_doctor_changes_whole_schedule_without_appointments(client, admin, make_doctor, auth, session):
    doctor = await make_doctor()

    response = await client.patch(
        f"{URL}/{doctor.id}",
        json={"slot_minutes": 15, "work_start": "10:00:00", "work_end": "14:00:00", "work_days": [6, 2]},
        headers=auth(admin),
    )

    assert response.status_code == 200
    stored = await reload_doctor(session, doctor.id)
    assert (stored.slot_minutes, stored.work_start, stored.work_end, stored.work_days) == (
        15,
        time(10),
        time(14),
        [2, 6],
    )


async def test_update_doctor_response_contains_full_name(client, admin, make_doctor, auth, session):
    doctor = await make_doctor()
    owner = await session.get(User, doctor.user_id)

    response = await client.patch(f"{URL}/{doctor.id}", json={"cabinet": "5"}, headers=auth(admin))

    assert response.json()["full_name"] == owner.full_name
    assert response.json()["user_id"] == doctor.user_id


async def test_update_doctor_empty_body_returns_422(client, admin, make_doctor, auth):
    doctor = await make_doctor()

    response = await client.patch(f"{URL}/{doctor.id}", json={}, headers=auth(admin))

    assert response.status_code == 422
    assert response.json() == {"detail": "No fields to update"}


async def test_update_nonexistent_doctor_returns_404(client, admin, auth):
    response = await client.patch(f"{URL}/9999", json={"cabinet": "5"}, headers=auth(admin))

    assert response.status_code == 404
    assert response.json() == {"detail": "Doctor not found"}


@pytest.mark.parametrize(
    "body",
    [
        {"user_id": 5},
        {"cabinet": None},
        {"specialty": None},
        {"slot_minutes": None},
        {"work_start": None},
        {"work_end": None},
        {"work_days": None},
        {"slot_minutes": 4},
        {"slot_minutes": 241},
        {"work_days": []},
        {"work_days": [1, 1]},
        {"work_days": [9]},
        {"work_start": "18:00:00", "work_end": "09:00:00"},
        {"work_start": "09:00:00", "work_end": "09:00:00"},
        {"work_start": "09:00:00+03:00"},
        {"cabinet": ""},
    ],
    ids=[
        "user_id-immutable",
        "null-cabinet",
        "null-specialty",
        "null-slot",
        "null-start",
        "null-end",
        "null-days",
        "slot-4",
        "slot-241",
        "days-empty",
        "days-duplicate",
        "day-9",
        "start-after-end",
        "start-equals-end",
        "start-with-offset",
        "empty-cabinet",
    ],
)
async def test_update_doctor_invalid_body_returns_422(client, admin, make_doctor, auth, body):
    doctor = await make_doctor()

    response = await client.patch(f"{URL}/{doctor.id}", json=body, headers=auth(admin))

    assert response.status_code == 422


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({"work_start": "18:00:00"}, "work_start must be earlier"),
        ({"work_start": "19:00:00"}, "work_start must be earlier"),
        ({"work_end": "09:00:00"}, "work_start must be earlier"),
        ({"work_end": "08:00:00"}, "work_start must be earlier"),
    ],
    ids=["start-equals-stored-end", "start-after-stored-end", "end-equals-stored-start", "end-before-stored-start"],
)
async def test_update_doctor_single_bound_conflicting_with_stored_one_returns_422(
    client, admin, make_doctor, auth, session, body, message
):
    doctor = await make_doctor()

    response = await client.patch(f"{URL}/{doctor.id}", json=body, headers=auth(admin))

    assert response.status_code == 422
    assert message in response.json()["detail"]
    stored = await reload_doctor(session, doctor.id)
    assert (stored.work_start, stored.work_end) == (time(9), time(18))


@pytest.mark.parametrize(
    "body",
    [{"work_start": "17:59:00"}, {"work_end": "09:01:00"}],
    ids=["start-1min-before-end", "end-1min-after-start"],
)
async def test_update_doctor_single_bound_one_minute_inside_is_accepted(client, admin, make_doctor, auth, body):
    doctor = await make_doctor()

    response = await client.patch(f"{URL}/{doctor.id}", json=body, headers=auth(admin))

    assert response.status_code == 200


# ---- смена графика при будущих записях ------------------------------------------


async def test_schedule_change_leaving_one_future_appointment_outside_shift_returns_409(
    client, admin, make_doctor, auth, session
):
    doctor = await make_doctor()
    patient = await make_patient(session)
    await book(session, doctor, patient, admin, msk(13, 14))  # вторник 14:00

    response = await client.patch(f"{URL}/{doctor.id}", json={"work_end": "12:00:00"}, headers=auth(admin))

    assert response.status_code == 409
    assert response.json()["detail"].startswith("The new schedule leaves 1 upcoming scheduled appointment(s)")
    assert (await reload_doctor(session, doctor.id)).work_end == time(18)


async def test_schedule_change_409_reports_exact_number_of_affected_appointments(
    client, admin, make_doctor, auth, session
):
    doctor = await make_doctor()
    patient = await make_patient(session)
    await book(session, doctor, patient, admin, msk(13, 14))
    await book(session, doctor, patient, admin, msk(14, 15))
    await book(session, doctor, patient, admin, msk(15, 16))
    await book(session, doctor, patient, admin, msk(13, 10))  # остаётся внутри смены

    response = await client.patch(f"{URL}/{doctor.id}", json={"work_end": "12:00:00"}, headers=auth(admin))

    assert response.status_code == 409
    assert "leaves 3 upcoming" in response.json()["detail"]


async def test_schedule_change_that_keeps_all_future_appointments_inside_succeeds(
    client, admin, make_doctor, auth, session
):
    doctor = await make_doctor()
    patient = await make_patient(session)
    await book(session, doctor, patient, admin, msk(13, 14))

    response = await client.patch(f"{URL}/{doctor.id}", json={"work_end": "14:30:00"}, headers=auth(admin))

    assert response.status_code == 200
    assert (await reload_doctor(session, doctor.id)).work_end == time(14, 30)


async def test_schedule_change_shift_end_equal_to_appointment_end_is_boundary_ok(
    client, admin, make_doctor, auth, session
):
    doctor = await make_doctor()
    patient = await make_patient(session)
    await book(session, doctor, patient, admin, msk(13, 14))  # 14:00-14:30

    ok = await client.patch(f"{URL}/{doctor.id}", json={"work_end": "14:30:00"}, headers=auth(admin))
    bad = await client.patch(f"{URL}/{doctor.id}", json={"work_end": "14:29:00"}, headers=auth(admin))

    assert ok.status_code == 200
    assert bad.status_code == 409


async def test_schedule_change_removing_workday_of_future_appointment_returns_409(
    client, admin, make_doctor, auth, session
):
    doctor = await make_doctor()
    patient = await make_patient(session)
    await book(session, doctor, patient, admin, msk(13, 10))  # вторник

    response = await client.patch(f"{URL}/{doctor.id}", json={"work_days": [1, 3, 4, 5]}, headers=auth(admin))

    assert response.status_code == 409
    assert "leaves 1 upcoming" in response.json()["detail"]
    assert (await reload_doctor(session, doctor.id)).work_days == [1, 2, 3, 4, 5]


async def test_schedule_change_removing_unrelated_workday_succeeds(client, admin, make_doctor, auth, session):
    doctor = await make_doctor()
    patient = await make_patient(session)
    await book(session, doctor, patient, admin, msk(13, 10))  # вторник

    response = await client.patch(f"{URL}/{doctor.id}", json={"work_days": [1, 2, 3, 4]}, headers=auth(admin))

    assert response.status_code == 200


async def test_slot_length_change_breaking_grid_returns_409(client, admin, make_doctor, auth, session):
    doctor = await make_doctor()
    patient = await make_patient(session)
    await book(session, doctor, patient, admin, msk(13, 10))  # 10:00-10:30, сетка 30 мин

    response = await client.patch(f"{URL}/{doctor.id}", json={"slot_minutes": 20}, headers=auth(admin))

    assert response.status_code == 409
    assert (await reload_doctor(session, doctor.id)).slot_minutes == 30


async def test_slot_length_change_keeping_grid_succeeds(client, admin, make_doctor, auth, session):
    doctor = await make_doctor()
    patient = await make_patient(session)
    await book(session, doctor, patient, admin, msk(13, 10))  # 30 мин = 2 слота по 15

    response = await client.patch(f"{URL}/{doctor.id}", json={"slot_minutes": 15}, headers=auth(admin))

    assert response.status_code == 200
    assert (await reload_doctor(session, doctor.id)).slot_minutes == 15


async def test_slot_length_change_making_appointment_longer_than_three_slots_returns_409(
    client, admin, make_doctor, auth, session
):
    doctor = await make_doctor()
    patient = await make_patient(session)
    await book(session, doctor, patient, admin, msk(13, 10), minutes=30)

    three_slots = await client.patch(
        f"{URL}/{doctor.id}", json={"slot_minutes": 10}, headers=auth(admin)
    )  # ровно 3 слота
    five_slots = await client.patch(f"{URL}/{doctor.id}", json={"slot_minutes": 5}, headers=auth(admin))  # 6 слотов

    assert three_slots.status_code == 200
    assert five_slots.status_code == 409


async def test_shift_start_moved_after_future_appointment_returns_409(client, admin, make_doctor, auth, session):
    doctor = await make_doctor()
    patient = await make_patient(session)
    await book(session, doctor, patient, admin, msk(13, 10))

    response = await client.patch(f"{URL}/{doctor.id}", json={"work_start": "10:30:00"}, headers=auth(admin))

    assert response.status_code == 409


@pytest.mark.parametrize(
    "status",
    [AppointmentStatus.CANCELLED, AppointmentStatus.COMPLETED, AppointmentStatus.NO_SHOW],
    ids=lambda s: s.value,
)
async def test_non_scheduled_appointments_do_not_block_schedule_change(
    client, admin, make_doctor, auth, session, status
):
    doctor = await make_doctor()
    patient = await make_patient(session)
    await book(session, doctor, patient, admin, msk(13, 14), status=status)

    response = await client.patch(f"{URL}/{doctor.id}", json={"work_end": "12:00:00"}, headers=auth(admin))

    assert response.status_code == 200


async def test_past_appointment_does_not_block_schedule_change(client, admin, make_doctor, auth, session):
    doctor = await make_doctor()
    patient = await make_patient(session)
    await book(session, doctor, patient, admin, msk(9, 14))  # прошлая пятница

    response = await client.patch(f"{URL}/{doctor.id}", json={"work_end": "12:00:00"}, headers=auth(admin))

    assert response.status_code == 200


async def test_appointment_starting_exactly_now_is_not_future(client, admin, make_doctor, auth, session):
    doctor = await make_doctor()
    patient = await make_patient(session)
    await book(session, doctor, patient, admin, FROZEN_NOW)  # 09:00:00 ровно

    response = await client.patch(f"{URL}/{doctor.id}", json={"work_start": "09:30:00"}, headers=auth(admin))

    assert response.status_code == 200


async def test_appointment_starting_one_second_after_now_is_future(client, admin, make_doctor, auth, session):
    doctor = await make_doctor()
    patient = await make_patient(session)
    await book(session, doctor, patient, admin, FROZEN_NOW + timedelta(seconds=1))

    response = await client.patch(f"{URL}/{doctor.id}", json={"work_start": "09:30:00"}, headers=auth(admin))

    assert response.status_code == 409


async def test_other_doctors_appointments_do_not_block_schedule_change(client, admin, make_doctor, auth, session):
    doctor = await make_doctor()
    other = await make_doctor()
    patient = await make_patient(session)
    await book(session, other, patient, admin, msk(13, 14))

    response = await client.patch(f"{URL}/{doctor.id}", json={"work_end": "12:00:00"}, headers=auth(admin))

    assert response.status_code == 200


async def test_cabinet_and_specialty_change_ignores_appointments_outside_current_grid(
    client, admin, make_doctor, auth, session
):
    doctor = await make_doctor()
    patient = await make_patient(session)
    await book(session, doctor, patient, admin, msk(13, 10, 7))  # вне сетки, график не меняется

    response = await client.patch(f"{URL}/{doctor.id}", json={"cabinet": "9", "specialty": "Лор"}, headers=auth(admin))

    assert response.status_code == 200


async def test_resending_identical_schedule_skips_appointment_check(client, admin, make_doctor, auth, session):
    doctor = await make_doctor()
    patient = await make_patient(session)
    await book(session, doctor, patient, admin, msk(13, 10, 7))  # вне сетки

    response = await client.patch(
        f"{URL}/{doctor.id}",
        json={"slot_minutes": 30, "work_start": "09:00:00", "work_end": "18:00:00", "work_days": [5, 4, 3, 2, 1]},
        headers=auth(admin),
    )

    assert response.status_code == 200


async def test_adding_workdays_with_future_appointments_succeeds(client, admin, make_doctor, auth, session):
    doctor = await make_doctor()
    patient = await make_patient(session)
    await book(session, doctor, patient, admin, msk(13, 10))

    response = await client.patch(f"{URL}/{doctor.id}", json={"work_days": [1, 2, 3, 4, 5, 6]}, headers=auth(admin))

    assert response.status_code == 200
    assert (await reload_doctor(session, doctor.id)).work_days == [1, 2, 3, 4, 5, 6]


async def test_get_now_override_moves_the_boundary_of_future_appointments(client, admin, make_doctor, auth, session):
    from app.main import app
    from app.routers.appointments import get_now

    doctor = await make_doctor()
    patient = await make_patient(session)
    await book(session, doctor, patient, admin, msk(13, 14))
    app.dependency_overrides[get_now] = lambda: msk(13, 15)  # приём уже в прошлом

    response = await client.patch(f"{URL}/{doctor.id}", json={"work_end": "12:00:00"}, headers=auth(admin))

    assert response.status_code == 200

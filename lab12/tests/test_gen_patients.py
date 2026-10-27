"""HTTP-тесты /api/patients: поиск, видимость, создание, изменение, привязка аккаунта."""

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.models import Appointment, AppointmentStatus, Patient, User, UserRole

URL = "/api/patients"
BASE_TIME = datetime(2026, 10, 13, 7, 0, tzinfo=UTC)
_seq = iter(range(1, 10_000))


def patient_body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "full_name": "Кузнецова Мария Ивановна",
        "birth_date": "1985-03-09",
        "phone": "+79161234567",
        "policy_number": "2000000000000001",
    }
    body.update(overrides)
    return body


async def make_patient(
    session, *, name: str = "Иванов Иван", policy: str | None = None, user=None, phone=None
) -> Patient:
    number = next(_seq)
    patient = Patient(
        user_id=user.id if user is not None else None,
        full_name=name,
        birth_date=date(1990, 1, 1),
        phone=phone,
        policy_number=policy or f"{3_000_000_000_000_000 + number}",
    )
    session.add(patient)
    await session.commit()
    return patient


async def book(session, doctor, patient, creator, status=AppointmentStatus.SCHEDULED, offset_hours=0) -> Appointment:
    start = BASE_TIME + timedelta(hours=offset_hours + next(_seq))
    appointment = Appointment(
        doctor_id=doctor.id,
        patient_id=patient.id,
        starts_at=start,
        ends_at=start + timedelta(minutes=30),
        status=status,
        created_by=creator.id,
    )
    session.add(appointment)
    await session.commit()
    return appointment


async def reload(session, patient_id: int) -> Patient:
    return await session.get(Patient, patient_id, populate_existing=True)


async def count_patients(session) -> int:
    return await session.scalar(select(func.count()).select_from(Patient))


@pytest.fixture
async def registrar(make_user):
    return await make_user(UserRole.REGISTRAR)


# --------------------------------------------------------------------------- #
# GET /api/patients (поиск)
# --------------------------------------------------------------------------- #


async def test_list_patients_registrar_sees_all_ordered_by_name_then_id(client, registrar, auth, session):
    yakov = await make_patient(session, name="Яковлев Яков")
    anna1 = await make_patient(session, name="Андреева Анна")
    anna2 = await make_patient(session, name="Андреева Анна")

    response = await client.get(URL, headers=auth(registrar))

    assert response.status_code == 200
    assert [p["id"] for p in response.json()] == [anna1.id, anna2.id, yakov.id]


async def test_list_patients_item_has_all_fields(client, registrar, auth, session):
    patient = await make_patient(session, name="Иванов Иван", policy="4000000000000001", phone="+79990001122")

    response = await client.get(URL, headers=auth(registrar))

    assert response.json() == [
        {
            "id": patient.id,
            "user_id": None,
            "full_name": "Иванов Иван",
            "birth_date": "1990-01-01",
            "phone": "+79990001122",
            "policy_number": "4000000000000001",
        }
    ]


@pytest.mark.parametrize(
    "query",
    ["петров", "ПЕТРОВ", "ПеТрОв", "ов петр", "  петров "],
    ids=["lower", "upper", "mixed", "inner-part", "padded"],
)
async def test_list_patients_search_by_name_ignores_case_for_cyrillic(client, registrar, auth, session, query):
    target = await make_patient(session, name="Сидоров Петров")
    await make_patient(session, name="Иванов Иван")

    response = await client.get(URL, params={"q": query}, headers=auth(registrar))

    assert [p["id"] for p in response.json()] == [target.id]


async def test_list_patients_search_by_policy_substring(client, registrar, auth, session):
    target = await make_patient(session, policy="5555123456789012")
    await make_patient(session, policy="6666666666666666")

    response = await client.get(URL, params={"q": "12345678"}, headers=auth(registrar))

    assert [p["id"] for p in response.json()] == [target.id]


@pytest.mark.parametrize("wildcard", ["%", "_"])
async def test_list_patients_search_treats_wildcards_literally(client, registrar, auth, session, wildcard):
    await make_patient(session, name="Иванов Иван")

    response = await client.get(URL, params={"q": wildcard}, headers=auth(registrar))

    assert response.json() == []


async def test_list_patients_blank_search_returns_all(client, registrar, auth, session):
    await make_patient(session)
    await make_patient(session)

    response = await client.get(URL, params={"q": "  "}, headers=auth(registrar))

    assert len(response.json()) == 2


async def test_list_patients_pagination(client, registrar, auth, session):
    patients = [await make_patient(session, name=f"Пациент {chr(1040 + i)}") for i in range(5)]

    page = await client.get(URL, params={"limit": 2, "offset": 1}, headers=auth(registrar))
    last = await client.get(URL, params={"limit": 100, "offset": 4}, headers=auth(registrar))

    assert [p["id"] for p in page.json()] == [patients[1].id, patients[2].id]
    assert [p["id"] for p in last.json()] == [patients[4].id]


@pytest.mark.parametrize(
    "params",
    [{"limit": 0}, {"limit": 101}, {"limit": "a"}, {"offset": -1}, {"offset": "a"}, {"q": ""}, {"q": "я" * 101}],
    ids=["limit-0", "limit-101", "limit-text", "offset-negative", "offset-text", "empty-q", "q-101"],
)
async def test_list_patients_invalid_query_returns_422(client, registrar, auth, params):
    response = await client.get(URL, params=params, headers=auth(registrar))

    assert response.status_code == 422


@pytest.mark.parametrize(
    "params",
    [{"limit": 1}, {"limit": 100}, {"offset": 0}, {"q": "я" * 100}],
    ids=["limit-1", "limit-100", "offset-0", "q-100"],
)
async def test_list_patients_boundary_query_is_accepted(client, registrar, auth, params):
    response = await client.get(URL, params=params, headers=auth(registrar))

    assert response.status_code == 200


async def test_list_patients_patient_role_gets_403(client, make_user, auth):
    user = await make_user(UserRole.PATIENT)

    response = await client.get(URL, headers=auth(user))

    assert response.status_code == 403
    assert response.json() == {"detail": "Insufficient permissions to list patients"}


# ---- видимость пациента для врача ------------------------------------------------


async def test_doctor_list_contains_only_own_patients(client, make_doctor, auth, session, make_user):
    doctor = await make_doctor()
    other_doctor = await make_doctor()
    creator = await make_user(UserRole.ADMIN)
    mine = await make_patient(session, name="Мой Пациент")
    theirs = await make_patient(session, name="Чужой Пациент")
    await make_patient(session, name="Без Записей")
    await book(session, doctor, mine, creator)
    await book(session, other_doctor, theirs, creator)
    doctor_user = await session.get(User, doctor.user_id)

    response = await client.get(URL, headers=auth(doctor_user))

    assert [p["id"] for p in response.json()] == [mine.id]


@pytest.mark.parametrize(
    ("status", "visible"),
    [
        (AppointmentStatus.SCHEDULED, True),
        (AppointmentStatus.COMPLETED, True),
        (AppointmentStatus.NO_SHOW, True),
        (AppointmentStatus.CANCELLED, False),
    ],
    ids=lambda v: getattr(v, "value", str(v)),
)
async def test_patient_visibility_for_doctor_depends_on_appointment_status(
    client, make_doctor, make_user, auth, session, status, visible
):
    doctor = await make_doctor()
    creator = await make_user(UserRole.ADMIN)
    patient = await make_patient(session)
    await book(session, doctor, patient, creator, status=status)
    doctor_user = await session.get(User, doctor.user_id)

    one = await client.get(f"{URL}/{patient.id}", headers=auth(doctor_user))
    many = await client.get(URL, headers=auth(doctor_user))

    assert one.status_code == (200 if visible else 404)
    assert [p["id"] for p in many.json()] == ([patient.id] if visible else [])
    if not visible:
        assert one.json() == {"detail": "Patient not found"}


async def test_patient_with_cancelled_and_active_appointments_is_visible_once(
    client, make_doctor, make_user, auth, session
):
    doctor = await make_doctor()
    creator = await make_user(UserRole.ADMIN)
    patient = await make_patient(session)
    await book(session, doctor, patient, creator, status=AppointmentStatus.CANCELLED)
    await book(session, doctor, patient, creator)
    await book(session, doctor, patient, creator)
    doctor_user = await session.get(User, doctor.user_id)

    response = await client.get(URL, headers=auth(doctor_user))

    assert [p["id"] for p in response.json()] == [patient.id]


async def test_doctor_search_does_not_leak_foreign_patients(client, make_doctor, make_user, auth, session):
    doctor = await make_doctor()
    creator = await make_user(UserRole.ADMIN)
    mine = await make_patient(session, name="Орлов Олег")
    await make_patient(session, name="Орлова Ольга")
    await book(session, doctor, mine, creator)
    doctor_user = await session.get(User, doctor.user_id)

    response = await client.get(URL, params={"q": "орлов"}, headers=auth(doctor_user))

    assert [p["id"] for p in response.json()] == [mine.id]


async def test_doctor_without_profile_sees_no_patients(client, make_user, auth, session):
    doctor_user = await make_user(UserRole.DOCTOR)
    patient = await make_patient(session)

    listing = await client.get(URL, headers=auth(doctor_user))
    one = await client.get(f"{URL}/{patient.id}", headers=auth(doctor_user))

    assert listing.json() == []
    assert one.status_code == 404


# --------------------------------------------------------------------------- #
# GET /api/patients/me, GET /api/patients/{id}
# --------------------------------------------------------------------------- #


async def test_get_my_patient_returns_own_profile(client, make_user, auth, session):
    user = await make_user(UserRole.PATIENT)
    own = await make_patient(session, name="Свой Пациент", user=user)
    await make_patient(session)

    response = await client.get(f"{URL}/me", headers=auth(user))

    assert response.status_code == 200
    assert response.json()["id"] == own.id
    assert response.json()["user_id"] == user.id
    assert response.json()["full_name"] == "Свой Пациент"


async def test_get_my_patient_without_profile_returns_404(client, make_user, auth):
    user = await make_user(UserRole.PATIENT)

    response = await client.get(f"{URL}/me", headers=auth(user))

    assert response.status_code == 404
    assert response.json() == {"detail": "Patient profile not found"}


async def test_patient_reads_own_record_by_id_and_foreign_one_is_404(client, make_user, auth, session):
    user = await make_user(UserRole.PATIENT)
    own = await make_patient(session, user=user)
    foreign = await make_patient(session)

    mine = await client.get(f"{URL}/{own.id}", headers=auth(user))
    other = await client.get(f"{URL}/{foreign.id}", headers=auth(user))
    missing = await client.get(f"{URL}/999999", headers=auth(user))

    assert mine.status_code == 200
    assert other.status_code == 404
    assert other.json() == missing.json() == {"detail": "Patient not found"}


async def test_patient_user_without_profile_cannot_read_any_patient(client, make_user, auth, session):
    user = await make_user(UserRole.PATIENT)
    foreign = await make_patient(session)

    response = await client.get(f"{URL}/{foreign.id}", headers=auth(user))

    assert response.status_code == 404


async def test_doctor_foreign_and_missing_patient_responses_are_indistinguishable(client, make_doctor, auth, session):
    doctor = await make_doctor()
    foreign = await make_patient(session)
    doctor_user = await session.get(User, doctor.user_id)

    foreign_resp = await client.get(f"{URL}/{foreign.id}", headers=auth(doctor_user))
    missing_resp = await client.get(f"{URL}/999999", headers=auth(doctor_user))

    assert foreign_resp.status_code == missing_resp.status_code == 404
    assert foreign_resp.json() == missing_resp.json()


async def test_registrar_reads_any_patient(client, registrar, auth, session):
    patient = await make_patient(session, name="Любой Пациент")

    response = await client.get(f"{URL}/{patient.id}", headers=auth(registrar))

    assert response.status_code == 200
    assert response.json()["full_name"] == "Любой Пациент"


async def test_get_patient_nonexistent_id_returns_404_for_staff(client, registrar, auth):
    response = await client.get(f"{URL}/999999", headers=auth(registrar))

    assert response.status_code == 404
    assert response.json() == {"detail": "Patient not found"}


@pytest.mark.parametrize(
    "patient_id", [0, -1, 2_147_483_648, "abc", "me2"], ids=["zero", "negative", "over-int32", "text", "me-like"]
)
async def test_get_patient_invalid_id_returns_422(client, registrar, auth, patient_id):
    response = await client.get(f"{URL}/{patient_id}", headers=auth(registrar))

    assert response.status_code == 422


async def test_get_my_patient_for_registrar_is_forbidden_not_validated_as_id(client, registrar, auth):
    response = await client.get(f"{URL}/me", headers=auth(registrar))

    assert response.status_code == 403


# --------------------------------------------------------------------------- #
# POST /api/patients
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.REGISTRAR], ids=lambda r: r.value)
async def test_create_patient_by_staff_stores_and_returns_data(client, make_user, auth, session, role):
    staff = await make_user(role)

    response = await client.post(URL, json=patient_body(), headers=auth(staff))

    assert response.status_code == 201
    body = response.json()
    assert body == {
        "id": body["id"],
        "user_id": None,
        "full_name": "Кузнецова Мария Ивановна",
        "birth_date": "1985-03-09",
        "phone": "+79161234567",
        "policy_number": "2000000000000001",
    }
    stored = await reload(session, body["id"])
    assert (stored.full_name, str(stored.birth_date), stored.phone, stored.policy_number, stored.user_id) == (
        "Кузнецова Мария Ивановна",
        "1985-03-09",
        "+79161234567",
        "2000000000000001",
        None,
    )


async def test_create_patient_without_phone_and_with_account_link(client, registrar, make_user, auth, session):
    account = await make_user(UserRole.PATIENT)
    body = patient_body(user_id=account.id)
    del body["phone"]

    response = await client.post(URL, json=body, headers=auth(registrar))

    assert response.status_code == 201
    assert response.json()["user_id"] == account.id
    assert response.json()["phone"] is None
    assert (await reload(session, response.json()["id"])).user_id == account.id


async def test_create_patient_trims_name_and_policy(client, registrar, auth):
    response = await client.post(
        URL, json=patient_body(full_name="  Иванов Иван ", policy_number=" 2000000000000001 "), headers=auth(registrar)
    )

    assert response.status_code == 201
    assert response.json()["full_name"] == "Иванов Иван"
    assert response.json()["policy_number"] == "2000000000000001"


async def test_create_patient_taken_policy_returns_409(client, registrar, auth, session):
    await make_patient(session, policy="2000000000000001")

    response = await client.post(URL, json=patient_body(), headers=auth(registrar))

    assert response.status_code == 409
    assert response.json() == {"detail": "A patient with this policy number already exists"}
    assert await count_patients(session) == 1


async def test_create_patient_taken_policy_is_reported_before_unknown_user(client, registrar, auth, session):
    await make_patient(session, policy="2000000000000001")

    response = await client.post(URL, json=patient_body(user_id=9999), headers=auth(registrar))

    assert response.status_code == 409


async def test_create_patient_with_nonexistent_user_returns_404(client, registrar, auth, session):
    response = await client.post(URL, json=patient_body(user_id=9999), headers=auth(registrar))

    assert response.status_code == 404
    assert response.json() == {"detail": "User not found"}
    assert await count_patients(session) == 0


@pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.REGISTRAR, UserRole.DOCTOR], ids=lambda r: r.value)
async def test_create_patient_linked_to_user_with_other_role_returns_422(
    client, registrar, make_user, auth, session, role
):
    account = await make_user(role)

    response = await client.post(URL, json=patient_body(user_id=account.id), headers=auth(registrar))

    assert response.status_code == 422
    assert response.json() == {"detail": "The linked user must have the 'patient' role"}
    assert await count_patients(session) == 0


async def test_create_patient_linked_to_user_already_linked_returns_409(client, registrar, make_user, auth, session):
    account = await make_user(UserRole.PATIENT)
    await make_patient(session, user=account)

    response = await client.post(URL, json=patient_body(user_id=account.id), headers=auth(registrar))

    assert response.status_code == 409
    assert response.json() == {"detail": "The user is already linked to another patient"}
    assert await count_patients(session) == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"full_name": "я"},
        {"full_name": "я" * 256},
        {"full_name": ""},
        {"full_name": None},
        {"birth_date": "2999-01-01"},
        {"birth_date": "1800-01-01"},
        {"birth_date": "2026-13-01"},
        {"birth_date": "09.03.1985"},
        {"birth_date": None},
        {"policy_number": "123456789012345"},
        {"policy_number": "12345678901234567"},
        {"policy_number": "20000000000000a1"},
        {"policy_number": None},
        {"phone": "89161234567"},
        {"phone": "+123456"},
        {"phone": "+1234567890123456"},
        {"phone": ""},
        {"user_id": 0},
        {"user_id": -3},
        {"user_id": 2_147_483_648},
        {"user_id": "a"},
        {"id": 1},
        {"extra": "x"},
    ],
    ids=[
        "name-1",
        "name-256",
        "name-empty",
        "name-null",
        "birth-future",
        "birth-too-old",
        "birth-bad-month",
        "birth-wrong-format",
        "birth-null",
        "policy-15",
        "policy-17",
        "policy-letter",
        "policy-null",
        "phone-no-plus",
        "phone-6-digits",
        "phone-16-digits",
        "phone-empty",
        "user-0",
        "user-negative",
        "user-over-int32",
        "user-text",
        "extra-id",
        "extra-field",
    ],
)
async def test_create_patient_invalid_body_returns_422(client, registrar, auth, session, overrides):
    response = await client.post(URL, json=patient_body(**overrides), headers=auth(registrar))

    assert response.status_code == 422
    assert await count_patients(session) == 0


@pytest.mark.parametrize("field", ["full_name", "birth_date", "policy_number"])
async def test_create_patient_missing_required_field_returns_422(client, registrar, auth, field):
    body = patient_body()
    del body[field]

    response = await client.post(URL, json=body, headers=auth(registrar))

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", field]


@pytest.mark.parametrize(
    "overrides",
    [
        {"full_name": "Яя"},
        {"full_name": "я" * 255},
        {"phone": "+1234567"},
        {"phone": "+123456789012345"},
        {"phone": None},
        {"user_id": None},
        {"birth_date": "1900-01-01"},
    ],
    ids=["name-2", "name-255", "phone-7-digits", "phone-15-digits", "phone-null", "user-null", "birth-1900"],
)
async def test_create_patient_boundary_values_are_accepted(client, registrar, auth, overrides):
    response = await client.post(URL, json=patient_body(**overrides), headers=auth(registrar))

    assert response.status_code == 201


# --------------------------------------------------------------------------- #
# PATCH /api/patients/{id}
# --------------------------------------------------------------------------- #


async def test_update_patient_changes_fields_and_keeps_others(client, registrar, auth, session):
    patient = await make_patient(session, name="Старое Имя", policy="7000000000000001", phone="+79990000000")

    response = await client.patch(
        f"{URL}/{patient.id}",
        json={"full_name": "Новое Имя", "birth_date": "1970-02-03", "policy_number": "7000000000000002"},
        headers=auth(registrar),
    )

    assert response.status_code == 200
    assert response.json() == {
        "id": patient.id,
        "user_id": None,
        "full_name": "Новое Имя",
        "birth_date": "1970-02-03",
        "phone": "+79990000000",
        "policy_number": "7000000000000002",
    }
    stored = await reload(session, patient.id)
    assert (stored.full_name, str(stored.birth_date), stored.phone, stored.policy_number) == (
        "Новое Имя",
        "1970-02-03",
        "+79990000000",
        "7000000000000002",
    )


async def test_update_patient_null_phone_clears_it(client, registrar, auth, session):
    patient = await make_patient(session, phone="+79990000000")

    response = await client.patch(f"{URL}/{patient.id}", json={"phone": None}, headers=auth(registrar))

    assert response.status_code == 200
    assert response.json()["phone"] is None
    assert (await reload(session, patient.id)).phone is None


async def test_update_patient_empty_body_returns_422(client, registrar, auth, session):
    patient = await make_patient(session)

    response = await client.patch(f"{URL}/{patient.id}", json={}, headers=auth(registrar))

    assert response.status_code == 422
    assert response.json() == {"detail": "No fields to update"}


async def test_update_nonexistent_patient_returns_404(client, registrar, auth):
    response = await client.patch(f"{URL}/999999", json={"phone": None}, headers=auth(registrar))

    assert response.status_code == 404
    assert response.json() == {"detail": "Patient not found"}


@pytest.mark.parametrize(
    "body",
    [
        {"full_name": None},
        {"birth_date": None},
        {"policy_number": None},
        {"full_name": "я"},
        {"birth_date": "2999-01-01"},
        {"policy_number": "123"},
        {"phone": "123"},
        {"user_id": 0},
        {"role": "x"},
    ],
    ids=[
        "null-name",
        "null-birth",
        "null-policy",
        "short-name",
        "future-birth",
        "short-policy",
        "bad-phone",
        "user-0",
        "extra-field",
    ],
)
async def test_update_patient_invalid_body_returns_422(client, registrar, auth, session, body):
    patient = await make_patient(session)

    response = await client.patch(f"{URL}/{patient.id}", json=body, headers=auth(registrar))

    assert response.status_code == 422


async def test_update_patient_policy_taken_by_other_returns_409_and_keeps_old(client, registrar, auth, session):
    patient = await make_patient(session, policy="7000000000000001")
    await make_patient(session, policy="7000000000000002")

    response = await client.patch(
        f"{URL}/{patient.id}", json={"policy_number": "7000000000000002"}, headers=auth(registrar)
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "A patient with this policy number already exists"}
    assert (await reload(session, patient.id)).policy_number == "7000000000000001"


async def test_update_patient_with_own_policy_is_not_a_conflict(client, registrar, auth, session):
    patient = await make_patient(session, policy="7000000000000001")

    response = await client.patch(
        f"{URL}/{patient.id}",
        json={"policy_number": "7000000000000001", "full_name": "Тот Же"},
        headers=auth(registrar),
    )

    assert response.status_code == 200


# ---- привязка аккаунта ------------------------------------------------------------


async def test_link_account_sets_user_id_and_patient_can_read_own_profile(client, registrar, make_user, auth, session):
    patient = await make_patient(session)
    account = await make_user(UserRole.PATIENT)

    response = await client.patch(f"{URL}/{patient.id}", json={"user_id": account.id}, headers=auth(registrar))
    me = await client.get(f"{URL}/me", headers=auth(account))

    assert response.status_code == 200
    assert response.json()["user_id"] == account.id
    assert (await reload(session, patient.id)).user_id == account.id
    assert me.status_code == 200
    assert me.json()["id"] == patient.id


async def test_unlink_account_with_null_user_id(client, registrar, make_user, auth, session):
    account = await make_user(UserRole.PATIENT)
    patient = await make_patient(session, user=account)

    response = await client.patch(f"{URL}/{patient.id}", json={"user_id": None}, headers=auth(registrar))
    me = await client.get(f"{URL}/me", headers=auth(account))

    assert response.status_code == 200
    assert response.json()["user_id"] is None
    assert (await reload(session, patient.id)).user_id is None
    assert me.status_code == 404


async def test_link_nonexistent_user_returns_404_and_changes_nothing(client, registrar, auth, session):
    patient = await make_patient(session)

    response = await client.patch(
        f"{URL}/{patient.id}", json={"user_id": 9999, "full_name": "Не Должно"}, headers=auth(registrar)
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "User not found"}
    stored = await reload(session, patient.id)
    assert (stored.user_id, stored.full_name) == (None, "Иванов Иван")


@pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.REGISTRAR, UserRole.DOCTOR], ids=lambda r: r.value)
async def test_link_user_with_other_role_returns_422(client, registrar, make_user, auth, session, role):
    patient = await make_patient(session)
    account = await make_user(role)

    response = await client.patch(f"{URL}/{patient.id}", json={"user_id": account.id}, headers=auth(registrar))

    assert response.status_code == 422
    assert response.json() == {"detail": "The linked user must have the 'patient' role"}
    assert (await reload(session, patient.id)).user_id is None


async def test_link_user_already_linked_to_another_patient_returns_409(client, registrar, make_user, auth, session):
    account = await make_user(UserRole.PATIENT)
    owner = await make_patient(session, user=account)
    other = await make_patient(session)

    response = await client.patch(f"{URL}/{other.id}", json={"user_id": account.id}, headers=auth(registrar))

    assert response.status_code == 409
    assert response.json() == {"detail": "The user is already linked to another patient"}
    assert (await reload(session, owner.id)).user_id == account.id
    assert (await reload(session, other.id)).user_id is None


async def test_relink_same_account_to_same_patient_is_a_noop(client, registrar, make_user, auth, session):
    account = await make_user(UserRole.PATIENT)
    patient = await make_patient(session, user=account)

    response = await client.patch(f"{URL}/{patient.id}", json={"user_id": account.id}, headers=auth(registrar))

    assert response.status_code == 200
    assert response.json()["user_id"] == account.id


async def test_replace_linked_account_with_another_frees_the_first(client, registrar, make_user, auth, session):
    first = await make_user(UserRole.PATIENT)
    second = await make_user(UserRole.PATIENT)
    patient = await make_patient(session, user=first)
    other = await make_patient(session)

    swap = await client.patch(f"{URL}/{patient.id}", json={"user_id": second.id}, headers=auth(registrar))
    reuse = await client.patch(f"{URL}/{other.id}", json={"user_id": first.id}, headers=auth(registrar))

    assert swap.status_code == 200
    assert reuse.status_code == 200
    assert (await reload(session, patient.id)).user_id == second.id
    assert (await reload(session, other.id)).user_id == first.id


async def test_registered_patient_cannot_be_linked_again_to_second_record(client, registrar, auth, session):
    register = await client.post(
        "/api/auth/register",
        json={
            "email": "self.reg@clinic.test",
            "password": "Password123",
            "full_name": "Сам Зарегистрировался",
            "birth_date": "1991-04-05",
            "policy_number": "8000000000000001",
        },
    )
    spare = await make_patient(session, policy="8000000000000002")

    response = await client.patch(f"{URL}/{spare.id}", json={"user_id": register.json()["id"]}, headers=auth(registrar))

    assert response.status_code == 409


# ---- роли ----------------------------------------------------------------------------


@pytest.mark.parametrize("role", [UserRole.DOCTOR, UserRole.PATIENT], ids=lambda r: r.value)
async def test_create_and_update_patient_forbidden_for_doctor_and_patient(client, make_user, auth, session, role):
    user = await make_user(role)
    patient = await make_patient(session)

    created = await client.post(URL, json=patient_body(), headers=auth(user))
    updated = await client.patch(f"{URL}/{patient.id}", json={"phone": None}, headers=auth(user))

    assert created.status_code == updated.status_code == 403
    assert created.json() == {"detail": "Insufficient permissions"}
    assert await count_patients(session) == 1

"""HTTP-тесты /api/users (только администратор) и последствия изменений для токенов."""

from datetime import date, time

import pytest
from conftest import PASSWORD
from sqlalchemy import func, select

from app.models import Doctor, Patient, User, UserRole
from app.security import verify_password

URL = "/api/users"
NEW_PASSWORD = "BrandNewPass456"


def user_body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "email": "created@clinic.test",
        "password": PASSWORD,
        "full_name": "Новый Сотрудник",
        "role": "registrar",
    }
    body.update(overrides)
    return body


async def reload(session, user_id: int) -> User:
    return await session.get(User, user_id, populate_existing=True)


async def rename(session, user: User, full_name: str) -> None:
    user.full_name = full_name
    await session.commit()


async def add_doctor_profile(session, user: User) -> Doctor:
    doctor = Doctor(user_id=user.id, specialty="Терапевт", cabinet="1", work_start=time(9), work_end=time(18))
    session.add(doctor)
    await session.commit()
    return doctor


async def add_patient_profile(session, user: User | None, policy: str = "1111111111111111") -> Patient:
    patient = Patient(
        user_id=user.id if user else None, full_name="Пациент", birth_date=date(1990, 1, 1), policy_number=policy
    )
    session.add(patient)
    await session.commit()
    return patient


# --------------------------------------------------------------------------- #
# GET /api/users
# --------------------------------------------------------------------------- #


async def test_list_users_returns_all_users_ordered_by_id_without_secrets(client, make_user, auth):
    admin = await make_user(UserRole.ADMIN)
    doctor = await make_user(UserRole.DOCTOR)
    patient = await make_user(UserRole.PATIENT)

    response = await client.get(URL, headers=auth(admin))

    assert response.status_code == 200
    body = response.json()
    assert [u["id"] for u in body] == [admin.id, doctor.id, patient.id]
    assert all(set(u) == {"id", "email", "full_name", "role", "is_active", "created_at"} for u in body)
    assert body[1]["role"] == "doctor"
    assert body[1]["email"] == doctor.email


async def test_list_users_filter_by_role_returns_only_that_role(client, make_user, auth):
    admin = await make_user(UserRole.ADMIN)
    doctor = await make_user(UserRole.DOCTOR)
    await make_user(UserRole.PATIENT)

    response = await client.get(URL, params={"role": "doctor"}, headers=auth(admin))

    assert [u["id"] for u in response.json()] == [doctor.id]


@pytest.mark.parametrize(("flag", "active"), [("true", True), ("false", False)])
async def test_list_users_filter_by_is_active(client, make_user, auth, flag, active):
    admin = await make_user(UserRole.ADMIN)
    inactive = await make_user(UserRole.DOCTOR, is_active=False)

    response = await client.get(URL, params={"is_active": flag}, headers=auth(admin))

    ids = [u["id"] for u in response.json()]
    assert (inactive.id in ids) is (not active)
    assert (admin.id in ids) is active
    assert all(u["is_active"] is active for u in response.json())


async def test_list_users_search_by_cyrillic_name_ignores_case(client, make_user, auth, session):
    admin = await make_user(UserRole.ADMIN)
    anna = await make_user(UserRole.PATIENT)
    boris = await make_user(UserRole.PATIENT)
    await rename(session, anna, "Петрова Анна")
    await rename(session, boris, "Сидоров Борис")

    upper = await client.get(URL, params={"q": "ПЕТРОВА"}, headers=auth(admin))
    lower = await client.get(URL, params={"q": "петрова"}, headers=auth(admin))
    part = await client.get(URL, params={"q": "ов бор"}, headers=auth(admin))

    assert [u["id"] for u in upper.json()] == [anna.id]
    assert [u["id"] for u in lower.json()] == [anna.id]
    assert [u["id"] for u in part.json()] == [boris.id]


async def test_list_users_search_by_email_ignores_case(client, make_user, auth):
    admin = await make_user(UserRole.ADMIN)
    target = await make_user(UserRole.DOCTOR, "Dr.House@Clinic.Test")

    response = await client.get(URL, params={"q": "DR.hOuSe"}, headers=auth(admin))

    assert [u["id"] for u in response.json()] == [target.id]


@pytest.mark.parametrize("wildcard", ["%", "_"])
async def test_list_users_search_treats_like_wildcards_literally(client, make_user, auth, wildcard):
    admin = await make_user(UserRole.ADMIN)
    await make_user(UserRole.DOCTOR)

    response = await client.get(URL, params={"q": wildcard}, headers=auth(admin))

    assert response.status_code == 200
    assert response.json() == []


async def test_list_users_blank_search_returns_everyone(client, make_user, auth):
    admin = await make_user(UserRole.ADMIN)
    await make_user(UserRole.DOCTOR)

    response = await client.get(URL, params={"q": "   "}, headers=auth(admin))

    assert len(response.json()) == 2


async def test_list_users_combined_filters_are_anded(client, make_user, auth):
    admin = await make_user(UserRole.ADMIN)
    await make_user(UserRole.DOCTOR, is_active=False)
    active_doctor = await make_user(UserRole.DOCTOR)

    response = await client.get(URL, params={"role": "doctor", "is_active": "true"}, headers=auth(admin))

    assert [u["id"] for u in response.json()] == [active_doctor.id]


async def test_list_users_pagination_returns_requested_window(client, make_user, auth):
    admin = await make_user(UserRole.ADMIN)
    others = [await make_user(UserRole.PATIENT) for _ in range(4)]
    everyone = [admin.id, *(u.id for u in others)]

    page = await client.get(URL, params={"limit": 2, "offset": 2}, headers=auth(admin))
    tail = await client.get(URL, params={"limit": 100, "offset": 4}, headers=auth(admin))
    beyond = await client.get(URL, params={"offset": 5}, headers=auth(admin))

    assert [u["id"] for u in page.json()] == everyone[2:4]
    assert [u["id"] for u in tail.json()] == everyone[4:]
    assert beyond.json() == []


async def test_list_users_default_limit_is_20(client, make_user, auth):
    admin = await make_user(UserRole.ADMIN)
    for _ in range(21):
        await make_user(UserRole.PATIENT)

    response = await client.get(URL, headers=auth(admin))

    assert len(response.json()) == 20


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 101},
        {"limit": -1},
        {"limit": "x"},
        {"offset": -1},
        {"offset": "x"},
        {"offset": 2_147_483_648},
        {"role": "nurse"},
        {"is_active": "maybe"},
        {"q": ""},
        {"q": "я" * 101},
    ],
    ids=[
        "limit-0",
        "limit-101",
        "limit-negative",
        "limit-text",
        "offset-negative",
        "offset-text",
        "offset-too-big",
        "unknown-role",
        "bad-bool",
        "empty-q",
        "q-101-chars",
    ],
)
async def test_list_users_invalid_query_returns_422(client, make_user, auth, params):
    admin = await make_user(UserRole.ADMIN)

    response = await client.get(URL, params=params, headers=auth(admin))

    assert response.status_code == 422


@pytest.mark.parametrize(
    "params",
    [{"limit": 1}, {"limit": 100}, {"offset": 0}, {"q": "я" * 100}],
    ids=["limit-1", "limit-100", "offset-0", "q-100-chars"],
)
async def test_list_users_boundary_query_is_accepted(client, make_user, auth, params):
    admin = await make_user(UserRole.ADMIN)

    response = await client.get(URL, params=params, headers=auth(admin))

    assert response.status_code == 200


# --------------------------------------------------------------------------- #
# POST /api/users
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("role", list(UserRole), ids=lambda r: r.value)
async def test_create_user_with_each_role_stores_active_user_with_hashed_password(
    client, make_user, auth, session, role
):
    admin = await make_user(UserRole.ADMIN)

    response = await client.post(URL, json=user_body(role=role.value), headers=auth(admin))

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"id", "email", "full_name", "role", "is_active", "created_at"}
    assert body["role"] == role.value
    assert body["is_active"] is True
    created = await reload(session, body["id"])
    assert created.email == "created@clinic.test"
    assert created.full_name == "Новый Сотрудник"
    assert created.password_hash != PASSWORD
    assert verify_password(PASSWORD, created.password_hash)
    assert created.token_version == 0


async def test_create_user_can_log_in_immediately(client, make_user, auth):
    admin = await make_user(UserRole.ADMIN)
    await client.post(URL, json=user_body(), headers=auth(admin))

    response = await client.post("/api/auth/login", json={"email": "created@clinic.test", "password": PASSWORD})

    assert response.status_code == 200


async def test_create_user_normalizes_email_and_name(client, make_user, auth):
    admin = await make_user(UserRole.ADMIN)

    response = await client.post(
        URL, json=user_body(email="  Mixed.Case@Clinic.Test ", full_name="  Иван Иванов  "), headers=auth(admin)
    )

    assert response.status_code == 201
    assert response.json()["email"] == "mixed.case@clinic.test"
    assert response.json()["full_name"] == "Иван Иванов"


@pytest.mark.parametrize("email", ["created@clinic.test", "CREATED@CLINIC.TEST"], ids=["same", "other-case"])
async def test_create_user_taken_email_returns_409_and_adds_nothing(client, make_user, auth, session, email):
    admin = await make_user(UserRole.ADMIN)
    await make_user(UserRole.DOCTOR, "created@clinic.test")

    response = await client.post(URL, json=user_body(email=email), headers=auth(admin))

    assert response.status_code == 409
    assert response.json() == {"detail": "Email is already registered"}
    assert await session.scalar(select(func.count()).select_from(User)) == 2


@pytest.mark.parametrize(
    "overrides",
    [
        {"role": "nurse"},
        {"role": None},
        {"role": "ADMIN"},
        {"is_active": False},
        {"token_version": 5},
        {"password": "short"},
        {"password": "a" * 73},
        {"email": "bad"},
        {"full_name": "x"},
        {"full_name": 123},
    ],
    ids=[
        "unknown-role",
        "null-role",
        "uppercase-role",
        "extra-is_active",
        "extra-token_version",
        "short-password",
        "long-password",
        "bad-email",
        "short-name",
        "numeric-name",
    ],
)
async def test_create_user_invalid_body_returns_422(client, make_user, auth, session, overrides):
    admin = await make_user(UserRole.ADMIN)

    response = await client.post(URL, json=user_body(**overrides), headers=auth(admin))

    assert response.status_code == 422
    assert await session.scalar(select(func.count()).select_from(User)) == 1


@pytest.mark.parametrize("field", ["email", "password", "full_name", "role"])
async def test_create_user_missing_field_returns_422(client, make_user, auth, field):
    admin = await make_user(UserRole.ADMIN)
    body = user_body()
    del body[field]

    response = await client.post(URL, json=body, headers=auth(admin))

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", field]


@pytest.mark.parametrize(
    ("password", "expected"),
    [("a" * 7, 422), ("a" * 8, 201), ("a" * 72, 201), ("a" * 73, 422)],
    ids=["7", "8", "72", "73"],
)
async def test_create_user_password_length_boundaries(client, make_user, auth, password, expected):
    admin = await make_user(UserRole.ADMIN)

    response = await client.post(URL, json=user_body(password=password), headers=auth(admin))

    assert response.status_code == expected


# --------------------------------------------------------------------------- #
# PATCH /api/users/{id}
# --------------------------------------------------------------------------- #


async def test_update_user_changes_name_email_and_active_flag(client, make_user, auth, session):
    admin = await make_user(UserRole.ADMIN)
    target = await make_user(UserRole.REGISTRAR)

    response = await client.patch(
        f"{URL}/{target.id}",
        json={"full_name": "Другое Имя", "email": "Other@Clinic.Test", "is_active": False},
        headers=auth(admin),
    )

    assert response.status_code == 200
    body = response.json()
    assert (body["id"], body["full_name"], body["email"], body["is_active"]) == (
        target.id,
        "Другое Имя",
        "other@clinic.test",
        False,
    )
    stored = await reload(session, target.id)
    assert (stored.full_name, stored.email, stored.is_active, stored.role) == (
        "Другое Имя",
        "other@clinic.test",
        False,
        UserRole.REGISTRAR,
    )


async def test_update_user_partial_patch_keeps_other_fields(client, make_user, auth, session):
    admin = await make_user(UserRole.ADMIN)
    target = await make_user(UserRole.REGISTRAR, "keep@clinic.test")
    old_name = target.full_name

    response = await client.patch(f"{URL}/{target.id}", json={"is_active": False}, headers=auth(admin))

    assert response.status_code == 200
    stored = await reload(session, target.id)
    assert (stored.email, stored.full_name, stored.role) == ("keep@clinic.test", old_name, UserRole.REGISTRAR)


async def test_update_user_empty_body_returns_422(client, make_user, auth):
    admin = await make_user(UserRole.ADMIN)
    target = await make_user(UserRole.REGISTRAR)

    response = await client.patch(f"{URL}/{target.id}", json={}, headers=auth(admin))

    assert response.status_code == 422
    assert response.json() == {"detail": "No fields to update"}


@pytest.mark.parametrize(
    "body",
    [{"email": None}, {"password": None}, {"full_name": None}, {"role": None}, {"is_active": None}],
    ids=["email", "password", "full_name", "role", "is_active"],
)
async def test_update_user_explicit_null_returns_422(client, make_user, auth, body):
    admin = await make_user(UserRole.ADMIN)
    target = await make_user(UserRole.REGISTRAR)

    response = await client.patch(f"{URL}/{target.id}", json=body, headers=auth(admin))

    assert response.status_code == 422


@pytest.mark.parametrize(
    "body",
    [
        {"token_version": 9},
        {"id": 5},
        {"password_hash": "x"},
        {"role": "nurse"},
        {"email": "bad"},
        {"full_name": "я"},
        {"is_active": "perhaps"},
        {"password": "short"},
    ],
    ids=["token_version", "id", "password_hash", "role", "email", "name", "bool", "password"],
)
async def test_update_user_invalid_body_returns_422(client, make_user, auth, body):
    admin = await make_user(UserRole.ADMIN)
    target = await make_user(UserRole.REGISTRAR)

    response = await client.patch(f"{URL}/{target.id}", json=body, headers=auth(admin))

    assert response.status_code == 422


async def test_update_nonexistent_user_returns_404(client, make_user, auth):
    admin = await make_user(UserRole.ADMIN)

    response = await client.patch(f"{URL}/99999", json={"full_name": "Имя Фамилия"}, headers=auth(admin))

    assert response.status_code == 404
    assert response.json() == {"detail": "User not found"}


@pytest.mark.parametrize("user_id", [0, -1, 2_147_483_648, "abc"], ids=["zero", "negative", "over-int32", "text"])
async def test_update_user_invalid_id_returns_422(client, make_user, auth, user_id):
    admin = await make_user(UserRole.ADMIN)

    response = await client.patch(f"{URL}/{user_id}", json={"full_name": "Имя Фамилия"}, headers=auth(admin))

    assert response.status_code == 422


async def test_update_user_email_taken_by_other_returns_409_and_keeps_email(client, make_user, auth, session):
    admin = await make_user(UserRole.ADMIN)
    target = await make_user(UserRole.REGISTRAR, "target@clinic.test")
    await make_user(UserRole.DOCTOR, "busy@clinic.test")

    response = await client.patch(f"{URL}/{target.id}", json={"email": "BUSY@clinic.test"}, headers=auth(admin))

    assert response.status_code == 409
    assert response.json() == {"detail": "Email is already registered"}
    assert (await reload(session, target.id)).email == "target@clinic.test"


async def test_update_user_own_email_in_other_case_is_not_a_conflict(client, make_user, auth, session):
    admin = await make_user(UserRole.ADMIN)
    target = await make_user(UserRole.REGISTRAR, "target@clinic.test")

    response = await client.patch(f"{URL}/{target.id}", json={"email": "Target@Clinic.Test"}, headers=auth(admin))

    assert response.status_code == 200
    assert (await reload(session, target.id)).email == "target@clinic.test"


# ---- защита администратора от самоблокировки ---------------------------------


@pytest.mark.parametrize("role", ["registrar", "doctor", "patient"])
async def test_admin_cannot_remove_own_admin_role(client, make_user, auth, session, role):
    admin = await make_user(UserRole.ADMIN)

    response = await client.patch(f"{URL}/{admin.id}", json={"role": role}, headers=auth(admin))

    assert response.status_code == 403
    assert response.json() == {"detail": "Administrators cannot remove their own admin role"}
    assert (await reload(session, admin.id)).role == UserRole.ADMIN


async def test_admin_cannot_deactivate_self(client, make_user, auth, session):
    admin = await make_user(UserRole.ADMIN)

    response = await client.patch(f"{URL}/{admin.id}", json={"is_active": False}, headers=auth(admin))

    assert response.status_code == 403
    assert response.json() == {"detail": "Administrators cannot deactivate themselves"}
    assert (await reload(session, admin.id)).is_active is True


async def test_admin_self_block_check_happens_before_any_change(client, make_user, auth, session):
    admin = await make_user(UserRole.ADMIN)
    old_name = admin.full_name

    response = await client.patch(
        f"{URL}/{admin.id}", json={"full_name": "Новое Имя", "role": "doctor"}, headers=auth(admin)
    )

    assert response.status_code == 403
    assert (await reload(session, admin.id)).full_name == old_name


@pytest.mark.parametrize(
    "body",
    [{"role": "admin"}, {"is_active": True}, {"full_name": "Главный Админ"}, {"email": "root@clinic.test"}],
    ids=["same-role", "stay-active", "rename", "email"],
)
async def test_admin_can_make_harmless_changes_to_self(client, make_user, auth, body):
    admin = await make_user(UserRole.ADMIN)

    response = await client.patch(f"{URL}/{admin.id}", json=body, headers=auth(admin))

    assert response.status_code == 200


async def test_admin_can_demote_and_deactivate_another_admin(client, make_user, auth, session):
    admin = await make_user(UserRole.ADMIN)
    other = await make_user(UserRole.ADMIN)

    response = await client.patch(
        f"{URL}/{other.id}", json={"role": "registrar", "is_active": False}, headers=auth(admin)
    )

    assert response.status_code == 200
    stored = await reload(session, other.id)
    assert (stored.role, stored.is_active) == (UserRole.REGISTRAR, False)


# ---- смена роли при существующем профиле -------------------------------------


@pytest.mark.parametrize("new_role", ["admin", "registrar", "patient"])
async def test_role_change_with_doctor_profile_returns_409(client, make_user, auth, session, new_role):
    admin = await make_user(UserRole.ADMIN)
    doctor_user = await make_user(UserRole.DOCTOR)
    await add_doctor_profile(session, doctor_user)

    response = await client.patch(f"{URL}/{doctor_user.id}", json={"role": new_role}, headers=auth(admin))

    assert response.status_code == 409
    assert response.json() == {"detail": f"The user has a doctor profile; the role cannot be changed to '{new_role}'"}
    assert (await reload(session, doctor_user.id)).role == UserRole.DOCTOR


@pytest.mark.parametrize("new_role", ["admin", "registrar", "doctor"])
async def test_role_change_with_linked_patient_returns_409(client, make_user, auth, session, new_role):
    admin = await make_user(UserRole.ADMIN)
    patient_user = await make_user(UserRole.PATIENT)
    await add_patient_profile(session, patient_user)

    response = await client.patch(f"{URL}/{patient_user.id}", json={"role": new_role}, headers=auth(admin))

    assert response.status_code == 409
    assert response.json() == {"detail": f"The user is linked to a patient; the role cannot be changed to '{new_role}'"}
    assert (await reload(session, patient_user.id)).role == UserRole.PATIENT


@pytest.mark.parametrize(
    ("old", "new"),
    [(UserRole.DOCTOR, UserRole.PATIENT), (UserRole.PATIENT, UserRole.DOCTOR), (UserRole.REGISTRAR, UserRole.ADMIN)],
    ids=["doctor-to-patient", "patient-to-doctor", "registrar-to-admin"],
)
async def test_role_change_without_profile_succeeds(client, make_user, auth, session, old, new):
    admin = await make_user(UserRole.ADMIN)
    target = await make_user(old)

    response = await client.patch(f"{URL}/{target.id}", json={"role": new.value}, headers=auth(admin))

    assert response.status_code == 200
    assert response.json()["role"] == new.value
    assert (await reload(session, target.id)).role == new


async def test_same_role_with_profile_is_not_a_conflict(client, make_user, auth, session):
    admin = await make_user(UserRole.ADMIN)
    doctor_user = await make_user(UserRole.DOCTOR)
    await add_doctor_profile(session, doctor_user)

    response = await client.patch(f"{URL}/{doctor_user.id}", json={"role": "doctor"}, headers=auth(admin))

    assert response.status_code == 200


# ---- токены после смены роли / пароля / деактивации ----------------------------


async def test_role_change_invalidates_issued_token(client, make_user, auth):
    admin = await make_user(UserRole.ADMIN)
    target = await make_user(UserRole.REGISTRAR)
    old_headers = auth(target)
    assert (await client.get("/api/auth/me", headers=old_headers)).status_code == 200

    await client.patch(f"{URL}/{target.id}", json={"role": "doctor"}, headers=auth(admin))
    response = await client.get("/api/auth/me", headers=old_headers)

    assert response.status_code == 401
    assert response.json() == {"detail": "Token is no longer valid"}


async def test_role_change_does_not_bump_token_version(client, make_user, auth, session):
    admin = await make_user(UserRole.ADMIN)
    target = await make_user(UserRole.REGISTRAR)

    await client.patch(f"{URL}/{target.id}", json={"role": "doctor"}, headers=auth(admin))

    assert (await reload(session, target.id)).token_version == 0


async def test_password_change_invalidates_old_token_and_old_password(client, make_user, auth, session):
    admin = await make_user(UserRole.ADMIN)
    target = await make_user(UserRole.REGISTRAR)
    old_headers = auth(target)

    patch = await client.patch(f"{URL}/{target.id}", json={"password": NEW_PASSWORD}, headers=auth(admin))
    old_token = await client.get("/api/auth/me", headers=old_headers)
    old_login = await client.post("/api/auth/login", json={"email": target.email, "password": PASSWORD})
    new_login = await client.post("/api/auth/login", json={"email": target.email, "password": NEW_PASSWORD})

    assert patch.status_code == 200
    assert "password" not in patch.json()
    assert old_token.status_code == 401
    assert old_token.json() == {"detail": "Token is no longer valid"}
    assert old_login.status_code == 401
    assert new_login.status_code == 200
    new_me = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {new_login.json()['access_token']}"})
    assert new_me.status_code == 200
    stored = await reload(session, target.id)
    assert stored.token_version == 1
    assert verify_password(NEW_PASSWORD, stored.password_hash)


async def test_two_password_changes_increment_token_version_twice(client, make_user, auth, session):
    admin = await make_user(UserRole.ADMIN)
    target = await make_user(UserRole.REGISTRAR)

    await client.patch(f"{URL}/{target.id}", json={"password": NEW_PASSWORD}, headers=auth(admin))
    await client.patch(f"{URL}/{target.id}", json={"password": "Another789Pass"}, headers=auth(admin))

    assert (await reload(session, target.id)).token_version == 2


async def test_non_password_change_keeps_issued_token_valid(client, make_user, auth):
    admin = await make_user(UserRole.ADMIN)
    target = await make_user(UserRole.REGISTRAR)
    headers = auth(target)

    await client.patch(f"{URL}/{target.id}", json={"full_name": "Новое Имя"}, headers=auth(admin))

    assert (await client.get("/api/auth/me", headers=headers)).status_code == 200


async def test_deactivation_invalidates_issued_token_and_blocks_login(client, make_user, auth):
    admin = await make_user(UserRole.ADMIN)
    target = await make_user(UserRole.REGISTRAR)
    headers = auth(target)

    await client.patch(f"{URL}/{target.id}", json={"is_active": False}, headers=auth(admin))
    me = await client.get("/api/auth/me", headers=headers)
    login = await client.post("/api/auth/login", json={"email": target.email, "password": PASSWORD})

    assert me.status_code == 401
    assert me.json() == {"detail": "Invalid or expired token"}
    assert login.status_code == 401
    assert login.json() == {"detail": "Incorrect email or password"}


async def test_reactivated_user_old_token_works_again_and_login_succeeds(client, make_user, auth):
    admin = await make_user(UserRole.ADMIN)
    target = await make_user(UserRole.REGISTRAR, is_active=False)

    await client.patch(f"{URL}/{target.id}", json={"is_active": True}, headers=auth(admin))
    login = await client.post("/api/auth/login", json={"email": target.email, "password": PASSWORD})

    assert login.status_code == 200

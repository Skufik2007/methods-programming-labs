"""HTTP-тесты /api/auth: регистрация пациента, вход, текущий пользователь."""

import pytest
from conftest import PASSWORD
from sqlalchemy import func, select

from app import models
from app.models import Patient, User, UserRole
from app.security import decode_access_token, verify_password

REGISTER_URL = "/api/auth/register"
LOGIN_URL = "/api/auth/login"
ME_URL = "/api/auth/me"
POLICY = "1234567890123456"
LOGIN_FAIL = {"detail": "Incorrect email or password"}


def register_body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "email": "new.patient@clinic.test",
        "password": PASSWORD,
        "full_name": "Петров Пётр Петрович",
        "birth_date": "1990-05-17",
        "phone": "+79991234567",
        "policy_number": POLICY,
    }
    body.update(overrides)
    return body


async def count(session, model) -> int:
    return await session.scalar(select(func.count()).select_from(model))


async def make_patient(session, *, policy: str = POLICY, user_id: int | None = None) -> Patient:
    from datetime import date

    patient = Patient(
        user_id=user_id, full_name="Сидоров Сидор", birth_date=date(1980, 2, 2), phone=None, policy_number=policy
    )
    session.add(patient)
    await session.commit()
    return patient


# --------------------------------------------------------------------------- #
# POST /api/auth/register
# --------------------------------------------------------------------------- #


async def test_register_valid_data_creates_patient_user_and_profile(client, session):
    response = await client.post(REGISTER_URL, json=register_body())

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"id", "email", "full_name", "role", "is_active", "created_at"}
    assert body["email"] == "new.patient@clinic.test"
    assert body["full_name"] == "Петров Пётр Петрович"
    assert body["role"] == "patient"
    assert body["is_active"] is True

    user = await session.get(User, body["id"], populate_existing=True)
    assert user.role == UserRole.PATIENT
    assert user.password_hash != PASSWORD
    assert verify_password(PASSWORD, user.password_hash)
    patient = await session.scalar(select(Patient).where(Patient.user_id == user.id))
    assert patient is not None
    assert patient.full_name == "Петров Пётр Петрович"
    assert str(patient.birth_date) == "1990-05-17"
    assert patient.phone == "+79991234567"
    assert patient.policy_number == POLICY


async def test_register_without_phone_stores_null_phone(client, session):
    body = register_body()
    del body["phone"]

    response = await client.post(REGISTER_URL, json=body)

    assert response.status_code == 201
    patient = await session.scalar(select(Patient).where(Patient.policy_number == POLICY))
    assert patient.phone is None


async def test_register_email_with_uppercase_and_spaces_is_normalized(client, session):
    response = await client.post(REGISTER_URL, json=register_body(email="  New.Patient@Clinic.TEST "))

    assert response.status_code == 201
    assert response.json()["email"] == "new.patient@clinic.test"


async def test_register_policy_with_surrounding_spaces_is_trimmed(client, session):
    response = await client.post(REGISTER_URL, json=register_body(policy_number=f" {POLICY} "))

    assert response.status_code == 201
    assert await session.scalar(select(Patient.id).where(Patient.policy_number == POLICY)) is not None


async def test_register_new_patient_can_then_log_in(client):
    await client.post(REGISTER_URL, json=register_body())

    response = await client.post(LOGIN_URL, json={"email": "new.patient@clinic.test", "password": PASSWORD})

    assert response.status_code == 200


async def test_register_taken_email_returns_409_and_creates_nothing(client, session, make_user):
    await make_user(UserRole.REGISTRAR, "new.patient@clinic.test")

    response = await client.post(REGISTER_URL, json=register_body())

    assert response.status_code == 409
    assert response.json() == {"detail": "Email is already registered"}
    assert await count(session, User) == 1
    assert await count(session, Patient) == 0


async def test_register_taken_email_in_other_case_returns_409(client, make_user):
    await make_user(UserRole.REGISTRAR, "new.patient@clinic.test")

    response = await client.post(REGISTER_URL, json=register_body(email="NEW.PATIENT@CLINIC.TEST"))

    assert response.status_code == 409
    assert response.json() == {"detail": "Email is already registered"}


async def test_register_taken_policy_returns_409_and_does_not_link_account(client, session):
    existing = await make_patient(session)

    response = await client.post(REGISTER_URL, json=register_body())

    assert response.status_code == 409
    assert response.json()["detail"].startswith("A patient with this policy number already exists")
    assert "registrar" in response.json()["detail"]
    await session.refresh(existing)
    assert existing.user_id is None  # чужая медкарта не привязана к новому аккаунту
    assert await count(session, User) == 0
    assert await count(session, Patient) == 1


async def test_register_taken_policy_of_patient_with_account_returns_409_and_keeps_owner(client, session, make_user):
    owner = await make_user(UserRole.PATIENT)
    existing = await make_patient(session, user_id=owner.id)

    response = await client.post(REGISTER_URL, json=register_body())

    assert response.status_code == 409
    await session.refresh(existing)
    assert existing.user_id == owner.id
    assert await count(session, User) == 1


async def test_register_taken_email_and_policy_reports_email_conflict_first(client, session, make_user):
    await make_user(UserRole.PATIENT, "new.patient@clinic.test")
    await make_patient(session)

    response = await client.post(REGISTER_URL, json=register_body())

    assert response.status_code == 409
    assert response.json() == {"detail": "Email is already registered"}


async def test_register_role_field_is_rejected_and_nothing_is_created(client, session):
    response = await client.post(REGISTER_URL, json=register_body(role="admin"))

    assert response.status_code == 422
    assert await count(session, User) == 0


@pytest.mark.parametrize("field", ["user_id", "is_active", "token_version", "id"])
async def test_register_extra_field_returns_422(client, field):
    response = await client.post(REGISTER_URL, json=register_body(**{field: 1}))

    assert response.status_code == 422
    assert response.json()["detail"][0]["type"] == "extra_forbidden"


@pytest.mark.parametrize("field", ["email", "password", "full_name", "birth_date", "policy_number"])
async def test_register_missing_required_field_returns_422(client, field):
    body = register_body()
    del body[field]

    response = await client.post(REGISTER_URL, json=body)

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", field]


@pytest.mark.parametrize(
    "email",
    ["", "plain", "a@b", "@clinic.test", "a b@clinic.test", "a@clinic.t", "a@@clinic.test", "x@.test"],
    ids=["empty", "no-at", "no-tld", "no-local", "space", "short-tld", "double-at", "dot-domain"],
)
async def test_register_invalid_email_returns_422(client, email):
    response = await client.post(REGISTER_URL, json=register_body(email=email))

    assert response.status_code == 422


async def test_register_email_of_254_chars_is_accepted_and_255_rejected(client):
    domain = ".".join(["a" * 60] * 3) + ".test"
    local254 = "u" * (254 - len(domain) - 1)
    ok = f"{local254}@{domain}"
    assert len(ok) == 254

    accepted = await client.post(REGISTER_URL, json=register_body(email=ok))
    rejected = await client.post(REGISTER_URL, json=register_body(email="u" + ok, policy_number="9999999999999999"))

    assert accepted.status_code == 201
    assert rejected.status_code == 422


@pytest.mark.parametrize(
    ("password", "expected"),
    [
        ("a" * 7, 422),
        ("a" * 8, 201),
        ("a" * 72, 201),
        ("a" * 73, 422),
        ("я" * 36, 201),  # 72 байта в UTF-8
        ("я" * 37, 422),  # 74 байта
        ("", 422),
    ],
    ids=["7-chars", "8-chars", "72-bytes", "73-bytes", "cyrillic-72-bytes", "cyrillic-74-bytes", "empty"],
)
async def test_register_password_length_boundaries(client, password, expected):
    response = await client.post(REGISTER_URL, json=register_body(password=password))

    assert response.status_code == expected


async def test_register_password_with_nul_character_is_not_truncated(client):
    registered = await client.post(REGISTER_URL, json=register_body(password="Pass\u0000word123"))
    email = "new.patient@clinic.test"

    same = await client.post(LOGIN_URL, json={"email": email, "password": "Pass\u0000word123"})
    prefix_only = await client.post(LOGIN_URL, json={"email": email, "password": "Pass\u0000other456"})

    assert registered.status_code == 201
    assert same.status_code == 200
    assert prefix_only.status_code == 401


@pytest.mark.parametrize(
    ("name", "expected"),
    [("a", 422), (" a ", 422), ("ab", 201), ("а" * 255, 201), ("а" * 256, 422), ("   ", 422), ("", 422)],
    ids=["1", "1-trimmed", "2", "255", "256", "spaces", "empty"],
)
async def test_register_full_name_length_boundaries(client, name, expected):
    response = await client.post(REGISTER_URL, json=register_body(full_name=name))

    assert response.status_code == expected


@pytest.mark.parametrize(
    "policy",
    ["123456789012345", "12345678901234567", "123456789012345a", "", "1234 5678 9012 34", "١٢٣٤٥٦٧٨٩٠١٢٣٤٥٦"],
    ids=["15-digits", "17-digits", "letter", "empty", "inner-spaces", "arabic-indic-digits"],
)
async def test_register_invalid_policy_returns_422(client, policy):
    response = await client.post(REGISTER_URL, json=register_body(policy_number=policy))

    assert response.status_code == 422


@pytest.mark.parametrize(
    ("phone", "expected"),
    [
        ("+1234567", 201),  # 7 цифр
        ("+123456", 422),  # 6 цифр
        ("+123456789012345", 201),  # 15 цифр
        ("+1234567890123456", 422),  # 16 цифр
        ("+0123456789", 422),
        ("89991234567", 422),
        ("+7 999 123 45 67", 422),
        (None, 201),
        ("", 422),
    ],
    ids=["7-digits", "6-digits", "15-digits", "16-digits", "leading-zero", "no-plus", "spaces", "null", "empty"],
)
async def test_register_phone_boundaries(client, phone, expected):
    response = await client.post(REGISTER_URL, json=register_body(phone=phone))

    assert response.status_code == expected


@pytest.mark.parametrize(
    "birth_date",
    ["2999-01-01", "1800-01-01", "2026-02-30", "17.05.1990", "", None],
    ids=["future", "too-old", "no-such-day", "wrong-format", "empty", "null"],
)
async def test_register_invalid_birth_date_returns_422(client, birth_date):
    response = await client.post(REGISTER_URL, json=register_body(birth_date=birth_date))

    assert response.status_code == 422


async def test_register_birth_date_of_adult_is_accepted(client, session):
    response = await client.post(REGISTER_URL, json=register_body(birth_date="1990-05-17"))

    assert response.status_code == 201


async def test_register_non_json_body_returns_422(client):
    response = await client.post(REGISTER_URL, content="not json", headers={"Content-Type": "application/json"})

    assert response.status_code == 422


# --------------------------------------------------------------------------- #
# POST /api/auth/login
# --------------------------------------------------------------------------- #


async def test_login_valid_credentials_returns_bearer_token_with_claims(client, make_user):
    user = await make_user(UserRole.DOCTOR)

    response = await client.post(LOGIN_URL, json={"email": user.email, "password": PASSWORD})

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"access_token", "token_type", "expires_in"}
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 3600
    payload = decode_access_token(body["access_token"])
    assert payload.user_id == user.id
    assert payload.role == UserRole.DOCTOR
    assert payload.version == user.token_version


async def test_login_issued_token_is_accepted_by_me(client, make_user):
    user = await make_user(UserRole.REGISTRAR)
    token = (await client.post(LOGIN_URL, json={"email": user.email, "password": PASSWORD})).json()["access_token"]

    response = await client.get(ME_URL, headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json()["id"] == user.id


@pytest.mark.parametrize("variant", ["upper", "padded"])
async def test_login_email_is_case_and_space_insensitive(client, make_user, variant):
    user = await make_user(UserRole.ADMIN, "admin.login@clinic.test")
    email = "ADMIN.Login@Clinic.Test" if variant == "upper" else "  admin.login@clinic.test  "

    response = await client.post(LOGIN_URL, json={"email": email, "password": PASSWORD})

    assert response.status_code == 200
    assert decode_access_token(response.json()["access_token"]).user_id == user.id


@pytest.mark.parametrize("reason", ["unknown-email", "wrong-password", "inactive-user", "inactive-wrong-password"])
async def test_login_failure_returns_identical_401(client, make_user, reason):
    inactive = reason.startswith("inactive")
    user = await make_user(UserRole.PATIENT, "known@clinic.test", is_active=not inactive)
    email = "nobody@clinic.test" if reason == "unknown-email" else user.email
    password = "WrongPassword1" if "wrong" in reason else PASSWORD

    response = await client.post(LOGIN_URL, json={"email": email, "password": password})

    assert response.status_code == 401
    assert response.json() == LOGIN_FAIL
    assert response.headers["www-authenticate"] == "Bearer"


async def test_login_deactivated_user_with_correct_password_gets_no_token(client, make_user):
    user = await make_user(UserRole.DOCTOR, is_active=False)

    response = await client.post(LOGIN_URL, json={"email": user.email, "password": PASSWORD})

    assert response.status_code == 401
    assert "access_token" not in response.json()


async def test_login_password_is_case_sensitive(client, make_user):
    user = await make_user(UserRole.ADMIN)

    response = await client.post(LOGIN_URL, json={"email": user.email, "password": PASSWORD.lower()})

    assert response.status_code == 401
    assert response.json() == LOGIN_FAIL


async def test_login_password_longer_than_72_bytes_returns_401_not_500(client, make_user):
    user = await make_user(UserRole.ADMIN)

    response = await client.post(LOGIN_URL, json={"email": user.email, "password": "a" * 100})

    assert response.status_code == 401
    assert response.json() == LOGIN_FAIL


async def test_login_password_with_nul_character_returns_401_not_500(client, make_user):
    user = await make_user(UserRole.ADMIN)

    response = await client.post(LOGIN_URL, json={"email": user.email, "password": "Pass\u0000word123"})

    assert response.status_code == 401


@pytest.mark.parametrize(
    ("length", "expected"), [(1, 401), (1024, 401), (1025, 422)], ids=["1-char", "1024-chars", "1025-chars"]
)
async def test_login_password_length_boundaries(client, make_user, length, expected):
    user = await make_user(UserRole.ADMIN)

    response = await client.post(LOGIN_URL, json={"email": user.email, "password": "x" * length})

    assert response.status_code == expected


async def test_login_empty_password_returns_422(client, make_user):
    user = await make_user(UserRole.ADMIN)

    response = await client.post(LOGIN_URL, json={"email": user.email, "password": ""})

    assert response.status_code == 422


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"email": "a@clinic.test"},
        {"password": PASSWORD},
        {"email": "bad", "password": PASSWORD},
        {"email": "a@clinic.test", "password": 12345678},
        {"email": None, "password": PASSWORD},
        {"email": "a@clinic.test", "password": PASSWORD, "remember": True},
    ],
    ids=["empty", "no-password", "no-email", "bad-email", "numeric-password", "null-email", "extra-field"],
)
async def test_login_invalid_body_returns_422(client, body):
    response = await client.post(LOGIN_URL, json=body)

    assert response.status_code == 422


async def test_login_does_not_change_token_version(client, make_user, session):
    user = await make_user(UserRole.ADMIN)

    await client.post(LOGIN_URL, json={"email": user.email, "password": PASSWORD})

    assert (await session.get(models.User, user.id, populate_existing=True)).token_version == 0


# --------------------------------------------------------------------------- #
# GET /api/auth/me
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("role", list(UserRole), ids=lambda r: r.value)
async def test_me_returns_current_user_without_secrets(client, make_user, auth, role):
    user = await make_user(role)

    response = await client.get(ME_URL, headers=auth(user))

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"id", "email", "full_name", "role", "is_active", "created_at"}
    assert body["id"] == user.id
    assert body["email"] == user.email
    assert body["full_name"] == user.full_name
    assert body["role"] == role.value
    assert body["is_active"] is True


async def test_me_without_token_returns_401(client):
    response = await client.get(ME_URL)

    assert response.status_code == 401
    assert response.json() == {"detail": "Not authenticated"}
    assert response.headers["www-authenticate"] == "Bearer"

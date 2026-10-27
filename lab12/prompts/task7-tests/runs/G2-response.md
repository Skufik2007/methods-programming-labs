Тесты написаны и прогнаны на текущем приложении: 564 теста, все проходят (SQLite). Ошибок приложения, требующих `xfail`, не найдено.

## Матрица доступа (из `require_roles` и проверок в сервисах)

| Эндпоинт | без токена | admin | registrar | doctor | patient |
|---|---|---|---|---|---|
| GET /api/auth/me | 401 | 200 | 200 | 200 | 200 |
| GET /api/users | 401 | 200 | 403 | 403 | 403 |
| POST /api/users | 401 | 201 | 403 | 403 | 403 |
| PATCH /api/users/{id} (есть / нет) | 401 | 200 / 404 | 403 | 403 | 403 |
| GET /api/doctors, /api/doctors/{id} | 401 | 200 (нет id: 404) | 200 | 200 | 200 |
| POST /api/doctors | 401 | 201 | 403 | 403 | 403 |
| PATCH /api/doctors/{id} | 401 | 200 / 404 | 403 | 403 | 403 |
| GET /api/patients | 401 | 200 | 200 | 200 (только свои) | 403 |
| GET /api/patients/me | 401 | 403 | 403 | 403 | 200 |
| GET /api/patients/{id}: свой / видимый | 401 | 200 | 200 | 200 | 200 |
| GET /api/patients/{id}: чужой | 401 | 200 | 200 | 404 | 404 |
| POST /api/patients | 401 | 201 | 201 | 403 | 403 |
| PATCH /api/patients/{id} (есть / нет) | 401 | 200 / 404 | 200 / 404 | 403 | 403 |

Роль проверяется раньше существования объекта: врач и пациент получают 403 и на несуществующий id.


### tests/test_gen_auth.py

```python
"""HTTP-тесты /api/auth: регистрация пациента, вход, текущий пользователь."""

import pytest
from sqlalchemy import func, select

from app import models
from app.models import Patient, User, UserRole
from app.security import decode_access_token, verify_password
from conftest import PASSWORD

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
    [{}, {"email": "a@clinic.test"}, {"password": PASSWORD}, {"email": "bad", "password": PASSWORD},
     {"email": "a@clinic.test", "password": 12345678}, {"email": None, "password": PASSWORD},
     {"email": "a@clinic.test", "password": PASSWORD, "remember": True}],
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
```

### tests/test_gen_security.py

```python
"""Зависимости get_current_user и require_roles (через GET /api/auth/me и закрытые эндпоинты)."""

import os
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.models import UserRole
from app.security import create_access_token, require_roles

ME_URL = "/api/auth/me"
USERS_URL = "/api/users"
SECRET = os.environ["JWT_SECRET"]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def forge(user, *, secret: str = SECRET, algorithm: str = "HS256", **overrides: object) -> str:
    claims: dict[str, object] = {
        "sub": str(user.id),
        "role": user.role.value,
        "ver": user.token_version,
        "exp": datetime.now(UTC) + timedelta(hours=1),
    }
    claims.update(overrides)
    for key in [k for k, v in claims.items() if v is ...]:
        del claims[key]
    return jwt.encode(claims, secret, algorithm=algorithm)


async def test_me_valid_token_returns_200(client, make_user):
    user = await make_user(UserRole.REGISTRAR)

    response = await client.get(ME_URL, headers=bearer(forge(user)))

    assert response.status_code == 200
    assert response.json()["id"] == user.id


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": ""}, {"Authorization": "Bearer"}, {"Authorization": "Bearer "},
     {"Authorization": "Basic dXNlcjpwYXNz"}, {"Authorization": "Token abc"}],
    ids=["no-header", "empty", "bearer-without-token", "bearer-space", "basic-scheme", "token-scheme"],
)
async def test_missing_or_non_bearer_credentials_return_not_authenticated(client, headers):
    response = await client.get(ME_URL, headers=headers)

    assert response.status_code == 401
    assert response.json() == {"detail": "Not authenticated"}
    assert response.headers["www-authenticate"] == "Bearer"


async def test_lowercase_bearer_scheme_is_accepted(client, make_user):
    user = await make_user(UserRole.ADMIN)

    response = await client.get(ME_URL, headers={"Authorization": f"bearer {forge(user)}"})

    assert response.status_code == 200


async def test_garbage_token_returns_invalid_token(client):
    response = await client.get(ME_URL, headers=bearer("not.a.jwt"))

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid or expired token"}
    assert response.headers["www-authenticate"] == "Bearer"


async def test_expired_token_returns_401(client, make_user):
    user = await make_user(UserRole.ADMIN)
    token = create_access_token(user.id, user.role, version=0, expires_delta=timedelta(seconds=-5))

    response = await client.get(ME_URL, headers=bearer(token))

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid or expired token"}


async def test_token_with_wrong_signature_returns_401(client, make_user):
    user = await make_user(UserRole.ADMIN)

    response = await client.get(ME_URL, headers=bearer(forge(user, secret="x" * 40)))

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid or expired token"}


async def test_token_with_other_hmac_algorithm_returns_401(client, make_user):
    user = await make_user(UserRole.ADMIN)

    response = await client.get(ME_URL, headers=bearer(forge(user, algorithm="HS512")))

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid or expired token"}


async def test_unsigned_alg_none_token_returns_401(client, make_user):
    user = await make_user(UserRole.ADMIN)
    token = jwt.encode(
        {"sub": str(user.id), "role": "admin", "ver": 0, "exp": datetime.now(UTC) + timedelta(hours=1)},
        key=None,
        algorithm="none",
    )

    response = await client.get(ME_URL, headers=bearer(token))

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid or expired token"}


@pytest.mark.parametrize("claim", ["sub", "role", "ver", "exp"])
async def test_token_without_required_claim_returns_401(client, make_user, claim):
    user = await make_user(UserRole.ADMIN)

    response = await client.get(ME_URL, headers=bearer(forge(user, **{claim: ...})))

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid or expired token"}


@pytest.mark.parametrize(
    "overrides",
    [{"sub": "abc"}, {"sub": "1.5"}, {"role": "superuser"}, {"role": 1}, {"ver": "0"}, {"ver": True}, {"ver": 0.0}],
    ids=["sub-not-number", "sub-float", "role-unknown", "role-number", "ver-string", "ver-bool", "ver-float"],
)
async def test_token_with_malformed_claim_returns_401(client, make_user, overrides):
    user = await make_user(UserRole.ADMIN)

    response = await client.get(ME_URL, headers=bearer(forge(user, **overrides)))

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid or expired token"}


async def test_token_of_nonexistent_user_returns_401(client, make_user):
    user = await make_user(UserRole.ADMIN)

    response = await client.get(ME_URL, headers=bearer(forge(user, sub=str(user.id + 1000))))

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid or expired token"}


async def test_token_of_deactivated_user_returns_401(client, make_user, auth):
    user = await make_user(UserRole.DOCTOR, is_active=False)

    response = await client.get(ME_URL, headers=auth(user))

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid or expired token"}


async def test_token_with_stale_role_returns_401(client, make_user):
    user = await make_user(UserRole.REGISTRAR)

    response = await client.get(ME_URL, headers=bearer(forge(user, role="admin")))

    assert response.status_code == 401
    assert response.json() == {"detail": "Token is no longer valid"}


@pytest.mark.parametrize("version", [0, 2, -1], ids=["older", "newer", "negative"])
async def test_token_with_version_different_from_db_returns_401(client, make_user, version):
    user = await make_user(UserRole.ADMIN, token_version=1)

    response = await client.get(ME_URL, headers=bearer(forge(user, ver=version)))

    assert response.status_code == 401
    assert response.json() == {"detail": "Token is no longer valid"}


async def test_token_with_matching_nonzero_version_is_accepted(client, make_user):
    user = await make_user(UserRole.ADMIN, token_version=3)

    response = await client.get(ME_URL, headers=bearer(forge(user)))

    assert response.status_code == 200


async def test_admin_token_is_rejected_on_admin_endpoint_after_deactivation_by_other_admin(client, make_user, auth):
    admin = await make_user(UserRole.ADMIN)
    target = await make_user(UserRole.ADMIN)
    assert (await client.get(USERS_URL, headers=auth(target))).status_code == 200

    patch = await client.patch(f"{USERS_URL}/{target.id}", json={"is_active": False}, headers=auth(admin))
    after = await client.get(USERS_URL, headers=auth(target))

    assert patch.status_code == 200
    assert after.status_code == 401
    assert after.json() == {"detail": "Invalid or expired token"}


async def test_unauthenticated_request_to_role_protected_endpoint_returns_401_not_403(client):
    response = await client.get(USERS_URL)

    assert response.status_code == 401
    assert response.json() == {"detail": "Not authenticated"}


@pytest.mark.parametrize("role", [UserRole.REGISTRAR, UserRole.DOCTOR, UserRole.PATIENT], ids=lambda r: r.value)
async def test_authenticated_user_with_foreign_role_gets_403(client, make_user, auth, role):
    user = await make_user(role)

    response = await client.get(USERS_URL, headers=auth(user))

    assert response.status_code == 403
    assert response.json() == {"detail": "Insufficient permissions"}


async def test_admin_has_no_implicit_access_to_patient_only_endpoint(client, make_user, auth):
    admin = await make_user(UserRole.ADMIN)

    response = await client.get("/api/patients/me", headers=auth(admin))

    assert response.status_code == 403
    assert response.json() == {"detail": "Insufficient permissions"}


def test_require_roles_without_roles_raises_value_error():
    with pytest.raises(ValueError, match="at least one role"):
        require_roles()


async def test_require_roles_dependency_allows_listed_role_and_rejects_other(make_user):
    from fastapi import HTTPException

    dependency = require_roles(UserRole.DOCTOR, UserRole.PATIENT)
    doctor = await make_user(UserRole.DOCTOR)
    admin = await make_user(UserRole.ADMIN)

    assert await dependency(doctor) is doctor
    with pytest.raises(HTTPException) as info:
        await dependency(admin)
    assert info.value.status_code == 403
    assert info.value.detail == "Insufficient permissions"
```

### tests/test_gen_users.py

```python
"""HTTP-тесты /api/users (только администратор) и последствия изменений для токенов."""

from datetime import date, time

import pytest
from sqlalchemy import func, select

from app.models import Doctor, Patient, User, UserRole
from app.security import verify_password
from conftest import PASSWORD

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
    [{"limit": 0}, {"limit": 101}, {"limit": -1}, {"limit": "x"}, {"offset": -1}, {"offset": "x"},
     {"offset": 2_147_483_648}, {"role": "nurse"}, {"is_active": "maybe"}, {"q": ""}, {"q": "я" * 101}],
    ids=["limit-0", "limit-101", "limit-negative", "limit-text", "offset-negative", "offset-text",
         "offset-too-big", "unknown-role", "bad-bool", "empty-q", "q-101-chars"],
)
async def test_list_users_invalid_query_returns_422(client, make_user, auth, params):
    admin = await make_user(UserRole.ADMIN)

    response = await client.get(URL, params=params, headers=auth(admin))

    assert response.status_code == 422


@pytest.mark.parametrize("params", [{"limit": 1}, {"limit": 100}, {"offset": 0}, {"q": "я" * 100}],
                         ids=["limit-1", "limit-100", "offset-0", "q-100-chars"])
async def test_list_users_boundary_query_is_accepted(client, make_user, auth, params):
    admin = await make_user(UserRole.ADMIN)

    response = await client.get(URL, params=params, headers=auth(admin))

    assert response.status_code == 200


# --------------------------------------------------------------------------- #
# POST /api/users
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("role", list(UserRole), ids=lambda r: r.value)
async def test_create_user_with_each_role_stores_active_user_with_hashed_password(client, make_user, auth, session, role):
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
    [{"role": "nurse"}, {"role": None}, {"role": "ADMIN"}, {"is_active": False}, {"token_version": 5},
     {"password": "short"}, {"password": "a" * 73}, {"email": "bad"}, {"full_name": "x"}, {"full_name": 123}],
    ids=["unknown-role", "null-role", "uppercase-role", "extra-is_active", "extra-token_version",
         "short-password", "long-password", "bad-email", "short-name", "numeric-name"],
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


@pytest.mark.parametrize(("password", "expected"), [("a" * 7, 422), ("a" * 8, 201), ("a" * 72, 201), ("a" * 73, 422)],
                         ids=["7", "8", "72", "73"])
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
        target.id, "Другое Имя", "other@clinic.test", False,
    )
    stored = await reload(session, target.id)
    assert (stored.full_name, stored.email, stored.is_active, stored.role) == (
        "Другое Имя", "other@clinic.test", False, UserRole.REGISTRAR,
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
    [{"token_version": 9}, {"id": 5}, {"password_hash": "x"}, {"role": "nurse"}, {"email": "bad"},
     {"full_name": "я"}, {"is_active": "perhaps"}, {"password": "short"}],
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

    response = await client.patch(f"{URL}/{other.id}", json={"role": "registrar", "is_active": False}, headers=auth(admin))

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
    assert response.json() == {
        "detail": f"The user has a doctor profile; the role cannot be changed to '{new_role}'"
    }
    assert (await reload(session, doctor_user.id)).role == UserRole.DOCTOR


@pytest.mark.parametrize("new_role", ["admin", "registrar", "doctor"])
async def test_role_change_with_linked_patient_returns_409(client, make_user, auth, session, new_role):
    admin = await make_user(UserRole.ADMIN)
    patient_user = await make_user(UserRole.PATIENT)
    await add_patient_profile(session, patient_user)

    response = await client.patch(f"{URL}/{patient_user.id}", json={"role": new_role}, headers=auth(admin))

    assert response.status_code == 409
    assert response.json() == {
        "detail": f"The user is linked to a patient; the role cannot be changed to '{new_role}'"
    }
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
    new_me = await client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {new_login.json()['access_token']}"}
    )
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
```

### tests/test_gen_doctors.py

```python
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


async def book(session, doctor, patient, creator, starts_at, minutes=30, status=AppointmentStatus.SCHEDULED, ends_at=None):
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


async def test_get_doctor_with_deactivated_account_is_still_readable_by_id(client, make_doctor, make_user, auth, session):
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


@pytest.mark.parametrize("query", ["терапевт", "ТЕРАПЕВТ", "Терапевт", "  терапевт  "],
                         ids=["lower", "upper", "exact", "padded"])
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


async def test_create_doctor_when_profile_exists_returns_409_and_keeps_old_profile(client, admin, make_doctor, auth, session):
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
    ids=["start-equals-end", "start-after-end", "start-with-offset", "end-before-start", "bad-time",
         "slot-4", "slot-241", "slot-0", "slot-text", "days-empty", "day-0", "day-8", "days-duplicate",
         "days-8-items", "days-not-list", "specialty-1", "specialty-101", "cabinet-empty", "cabinet-21",
         "user-0", "user-over-int32", "user-text", "extra-field"],
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
    [{"slot_minutes": 5}, {"slot_minutes": 240}, {"work_days": [1, 2, 3, 4, 5, 6, 7]}, {"work_days": [7]},
     {"specialty": "Яя"}, {"specialty": "я" * 100}, {"cabinet": "1"}, {"cabinet": "1" * 20},
     {"work_start": "00:00:00", "work_end": "00:01:00"}, {"work_start": "23:58:00", "work_end": "23:59:00"}],
    ids=["slot-5", "slot-240", "all-days", "sunday-only", "specialty-2", "specialty-100", "cabinet-1",
         "cabinet-20", "one-minute-at-midnight", "one-minute-before-midnight"],
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
        30, time(9), time(18), [1, 2, 3, 4, 5],
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
        15, time(10), time(14), [2, 6],
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
    [{"user_id": 5}, {"cabinet": None}, {"specialty": None}, {"slot_minutes": None}, {"work_start": None},
     {"work_end": None}, {"work_days": None}, {"slot_minutes": 4}, {"slot_minutes": 241}, {"work_days": []},
     {"work_days": [1, 1]}, {"work_days": [9]}, {"work_start": "18:00:00", "work_end": "09:00:00"},
     {"work_start": "09:00:00", "work_end": "09:00:00"}, {"work_start": "09:00:00+03:00"}, {"cabinet": ""}],
    ids=["user_id-immutable", "null-cabinet", "null-specialty", "null-slot", "null-start", "null-end",
         "null-days", "slot-4", "slot-241", "days-empty", "days-duplicate", "day-9", "start-after-end",
         "start-equals-end", "start-with-offset", "empty-cabinet"],
)
async def test_update_doctor_invalid_body_returns_422(client, admin, make_doctor, auth, body):
    doctor = await make_doctor()

    response = await client.patch(f"{URL}/{doctor.id}", json=body, headers=auth(admin))

    assert response.status_code == 422


@pytest.mark.parametrize(
    ("body", "message"),
    [({"work_start": "18:00:00"}, "work_start must be earlier"), ({"work_start": "19:00:00"}, "work_start must be earlier"),
     ({"work_end": "09:00:00"}, "work_start must be earlier"), ({"work_end": "08:00:00"}, "work_start must be earlier")],
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


@pytest.mark.parametrize("body", [{"work_start": "17:59:00"}, {"work_end": "09:01:00"}], ids=["start-1min-before-end", "end-1min-after-start"])
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


async def test_schedule_change_409_reports_exact_number_of_affected_appointments(client, admin, make_doctor, auth, session):
    doctor = await make_doctor()
    patient = await make_patient(session)
    await book(session, doctor, patient, admin, msk(13, 14))
    await book(session, doctor, patient, admin, msk(14, 15))
    await book(session, doctor, patient, admin, msk(15, 16))
    await book(session, doctor, patient, admin, msk(13, 10))  # остаётся внутри смены

    response = await client.patch(f"{URL}/{doctor.id}", json={"work_end": "12:00:00"}, headers=auth(admin))

    assert response.status_code == 409
    assert "leaves 3 upcoming" in response.json()["detail"]


async def test_schedule_change_that_keeps_all_future_appointments_inside_succeeds(client, admin, make_doctor, auth, session):
    doctor = await make_doctor()
    patient = await make_patient(session)
    await book(session, doctor, patient, admin, msk(13, 14))

    response = await client.patch(f"{URL}/{doctor.id}", json={"work_end": "14:30:00"}, headers=auth(admin))

    assert response.status_code == 200
    assert (await reload_doctor(session, doctor.id)).work_end == time(14, 30)


async def test_schedule_change_shift_end_equal_to_appointment_end_is_boundary_ok(client, admin, make_doctor, auth, session):
    doctor = await make_doctor()
    patient = await make_patient(session)
    await book(session, doctor, patient, admin, msk(13, 14))  # 14:00-14:30

    ok = await client.patch(f"{URL}/{doctor.id}", json={"work_end": "14:30:00"}, headers=auth(admin))
    bad = await client.patch(f"{URL}/{doctor.id}", json={"work_end": "14:29:00"}, headers=auth(admin))

    assert ok.status_code == 200
    assert bad.status_code == 409


async def test_schedule_change_removing_workday_of_future_appointment_returns_409(client, admin, make_doctor, auth, session):
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


async def test_slot_length_change_making_appointment_longer_than_three_slots_returns_409(client, admin, make_doctor, auth, session):
    doctor = await make_doctor()
    patient = await make_patient(session)
    await book(session, doctor, patient, admin, msk(13, 10), minutes=30)

    three_slots = await client.patch(f"{URL}/{doctor.id}", json={"slot_minutes": 10}, headers=auth(admin))  # ровно 3 слота
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
    "status", [AppointmentStatus.CANCELLED, AppointmentStatus.COMPLETED, AppointmentStatus.NO_SHOW],
    ids=lambda s: s.value,
)
async def test_non_scheduled_appointments_do_not_block_schedule_change(client, admin, make_doctor, auth, session, status):
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


async def test_cabinet_and_specialty_change_ignores_appointments_outside_current_grid(client, admin, make_doctor, auth, session):
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
```

### tests/test_gen_patients.py

```python
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


async def make_patient(session, *, name: str = "Иванов Иван", policy: str | None = None, user=None, phone=None) -> Patient:
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
        doctor_id=doctor.id, patient_id=patient.id, starts_at=start, ends_at=start + timedelta(minutes=30),
        status=status, created_by=creator.id,
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

    assert response.json() == [{
        "id": patient.id, "user_id": None, "full_name": "Иванов Иван", "birth_date": "1990-01-01",
        "phone": "+79990001122", "policy_number": "4000000000000001",
    }]


@pytest.mark.parametrize("query", ["петров", "ПЕТРОВ", "ПеТрОв", "ов петр", "  петров "],
                         ids=["lower", "upper", "mixed", "inner-part", "padded"])
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


@pytest.mark.parametrize("params", [{"limit": 1}, {"limit": 100}, {"offset": 0}, {"q": "я" * 100}],
                         ids=["limit-1", "limit-100", "offset-0", "q-100"])
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
    [(AppointmentStatus.SCHEDULED, True), (AppointmentStatus.COMPLETED, True), (AppointmentStatus.NO_SHOW, True),
     (AppointmentStatus.CANCELLED, False)],
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


async def test_patient_with_cancelled_and_active_appointments_is_visible_once(client, make_doctor, make_user, auth, session):
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


@pytest.mark.parametrize("patient_id", [0, -1, 2_147_483_648, "abc", "me2"],
                         ids=["zero", "negative", "over-int32", "text", "me-like"])
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
        "id": body["id"], "user_id": None, "full_name": "Кузнецова Мария Ивановна",
        "birth_date": "1985-03-09", "phone": "+79161234567", "policy_number": "2000000000000001",
    }
    stored = await reload(session, body["id"])
    assert (stored.full_name, str(stored.birth_date), stored.phone, stored.policy_number, stored.user_id) == (
        "Кузнецова Мария Ивановна", "1985-03-09", "+79161234567", "2000000000000001", None,
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
async def test_create_patient_linked_to_user_with_other_role_returns_422(client, registrar, make_user, auth, session, role):
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
        {"full_name": "я"}, {"full_name": "я" * 256}, {"full_name": ""}, {"full_name": None},
        {"birth_date": "2999-01-01"}, {"birth_date": "1800-01-01"}, {"birth_date": "2026-13-01"},
        {"birth_date": "09.03.1985"}, {"birth_date": None},
        {"policy_number": "123456789012345"}, {"policy_number": "12345678901234567"},
        {"policy_number": "20000000000000a1"}, {"policy_number": None},
        {"phone": "89161234567"}, {"phone": "+123456"}, {"phone": "+1234567890123456"}, {"phone": ""},
        {"user_id": 0}, {"user_id": -3}, {"user_id": 2_147_483_648}, {"user_id": "a"},
        {"id": 1}, {"extra": "x"},
    ],
    ids=["name-1", "name-256", "name-empty", "name-null", "birth-future", "birth-too-old", "birth-bad-month",
         "birth-wrong-format", "birth-null", "policy-15", "policy-17", "policy-letter", "policy-null",
         "phone-no-plus", "phone-6-digits", "phone-16-digits", "phone-empty", "user-0", "user-negative",
         "user-over-int32", "user-text", "extra-id", "extra-field"],
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
    [{"full_name": "Яя"}, {"full_name": "я" * 255}, {"phone": "+1234567"}, {"phone": "+123456789012345"},
     {"phone": None}, {"user_id": None}, {"birth_date": "1900-01-01"}],
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
        "id": patient.id, "user_id": None, "full_name": "Новое Имя", "birth_date": "1970-02-03",
        "phone": "+79990000000", "policy_number": "7000000000000002",
    }
    stored = await reload(session, patient.id)
    assert (stored.full_name, str(stored.birth_date), stored.phone, stored.policy_number) == (
        "Новое Имя", "1970-02-03", "+79990000000", "7000000000000002",
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
    [{"full_name": None}, {"birth_date": None}, {"policy_number": None}, {"full_name": "я"},
     {"birth_date": "2999-01-01"}, {"policy_number": "123"}, {"phone": "123"}, {"user_id": 0}, {"role": "x"}],
    ids=["null-name", "null-birth", "null-policy", "short-name", "future-birth", "short-policy", "bad-phone",
         "user-0", "extra-field"],
)
async def test_update_patient_invalid_body_returns_422(client, registrar, auth, session, body):
    patient = await make_patient(session)

    response = await client.patch(f"{URL}/{patient.id}", json=body, headers=auth(registrar))

    assert response.status_code == 422


async def test_update_patient_policy_taken_by_other_returns_409_and_keeps_old(client, registrar, auth, session):
    patient = await make_patient(session, policy="7000000000000001")
    await make_patient(session, policy="7000000000000002")

    response = await client.patch(f"{URL}/{patient.id}", json={"policy_number": "7000000000000002"}, headers=auth(registrar))

    assert response.status_code == 409
    assert response.json() == {"detail": "A patient with this policy number already exists"}
    assert (await reload(session, patient.id)).policy_number == "7000000000000001"


async def test_update_patient_with_own_policy_is_not_a_conflict(client, registrar, auth, session):
    patient = await make_patient(session, policy="7000000000000001")

    response = await client.patch(
        f"{URL}/{patient.id}", json={"policy_number": "7000000000000001", "full_name": "Тот Же"}, headers=auth(registrar)
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

    response = await client.patch(f"{URL}/{patient.id}", json={"user_id": 9999, "full_name": "Не Должно"}, headers=auth(registrar))

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
    register = await client.post("/api/auth/register", json={
        "email": "self.reg@clinic.test", "password": "Password123", "full_name": "Сам Зарегистрировался",
        "birth_date": "1991-04-05", "policy_number": "8000000000000001",
    })
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
```

### tests/test_gen_access_matrix.py

```python
"""Матрица доступа: эндпоинт x роль (без токена, admin, registrar, doctor, patient)."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Callable

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
    _case("POST", "/api/users", lambda w: {"email": "n@clinic.test", "password": "Password123",
                                           "full_name": "Новый Человек", "role": "registrar"},
          (401, 201, 403, 403, 403)),
    _case("PATCH", lambda w: f"/api/users/{w.spare_user.id}", lambda w: {"full_name": "Другое Имя"},
          (401, 200, 403, 403, 403), "PATCH /api/users/{existing}"),
    _case("PATCH", "/api/users/999999", lambda w: {"full_name": "Другое Имя"}, (401, 404, 403, 403, 403)),
    _case("GET", "/api/doctors", lambda w: None, (401, 200, 200, 200, 200)),
    _case("GET", lambda w: f"/api/doctors/{w.doctor.id}", lambda w: None, (401, 200, 200, 200, 200),
          "GET /api/doctors/{existing}"),
    _case("GET", "/api/doctors/999999", lambda w: None, (401, 404, 404, 404, 404)),
    _case("POST", "/api/doctors", lambda w: {"user_id": w.new_doctor_user.id, "specialty": "Лор", "cabinet": "3",
                                            "work_start": "09:00:00", "work_end": "17:00:00"},
          (401, 201, 403, 403, 403)),
    _case("PATCH", lambda w: f"/api/doctors/{w.doctor.id}", lambda w: {"cabinet": "202"},
          (401, 200, 403, 403, 403), "PATCH /api/doctors/{existing}"),
    _case("PATCH", "/api/doctors/999999", lambda w: {"cabinet": "202"}, (401, 404, 403, 403, 403)),
    _case("GET", "/api/patients", lambda w: None, (401, 200, 200, 200, 403)),
    _case("GET", "/api/patients/me", lambda w: None, (401, 403, 403, 403, 200)),
    _case("GET", lambda w: f"/api/patients/{w.own_patient.id}", lambda w: None, (401, 200, 200, 200, 200),
          "GET /api/patients/{own-or-visible}"),
    _case("GET", lambda w: f"/api/patients/{w.foreign_patient.id}", lambda w: None, (401, 200, 200, 404, 404),
          "GET /api/patients/{foreign}"),
    _case("GET", "/api/patients/999999", lambda w: None, (401, 404, 404, 404, 404)),
    _case("POST", "/api/patients", lambda w: {"full_name": "Новый Пациент", "birth_date": "1990-01-01",
                                             "policy_number": "9000000000000001"},
          (401, 201, 201, 403, 403)),
    _case("PATCH", lambda w: f"/api/patients/{w.own_patient.id}", lambda w: {"phone": "+79990000000"},
          (401, 200, 200, 403, 403), "PATCH /api/patients/{existing}"),
    _case("PATCH", "/api/patients/999999", lambda w: {"phone": "+79990000000"}, (401, 404, 404, 403, 403)),
]


@pytest.fixture
async def world(session, make_user, make_doctor):
    admin = await make_user(UserRole.ADMIN)
    registrar = await make_user(UserRole.REGISTRAR)
    doctor = await make_doctor()
    doctor_user = await session.get(User, doctor.user_id)
    patient_user = await make_user(UserRole.PATIENT)
    own = Patient(user_id=patient_user.id, full_name="Свой Пациент", birth_date=date(1990, 1, 1),
                  policy_number="1000000000000001")
    foreign = Patient(full_name="Чужой Пациент", birth_date=date(1991, 2, 2), policy_number="1000000000000002")
    session.add_all([own, foreign])
    await session.commit()
    start = datetime(2026, 10, 13, 7, 0, tzinfo=UTC)
    session.add(Appointment(doctor_id=doctor.id, patient_id=own.id, starts_at=start,
                            ends_at=start + timedelta(minutes=30), status=AppointmentStatus.SCHEDULED,
                            created_by=admin.id))
    await session.commit()
    return SimpleNamespace(
        users={"admin": admin, "registrar": registrar, "doctor": doctor_user, "patient": patient_user},
        doctor=doctor, own_patient=own, foreign_patient=foreign,
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
```

## Что покрыто

| Файл / функции | Сценарии |
|---|---|
| test_gen_auth.py: register | успех (тело и БД: User + Patient, bcrypt-хэш), нормализация email и полиса, вход после регистрации; занятый email (в т. ч. в другом регистре), занятый полис без привязки к чужой медкарте (владелец не меняется, новый User не создаётся), приоритет конфликта email; лишние поля (`role`, `is_active`...), обязательные поля, границы пароля (7/8/72/73 байта, кириллица 72/74), имени (1/2/255/256), телефона (6/7/15/16 цифр), полиса, даты рождения, email 254/255; NUL в пароле |
| test_gen_auth.py: login | claims токена, `expires_in`, регистр и пробелы в email; одинаковый 401 (тело и `WWW-Authenticate`) для неизвестного email, неверного пароля, деактивированного; пароль > 72 байт и с NUL, границы 1/1024/1025; невалидные тела |
| test_gen_auth.py: me | все четыре роли, поля без секретов, 401 без токена |
| test_gen_security.py | `get_current_user`: нет/пустой/не Bearer заголовок, мусорный, просроченный токен, чужая подпись, HS512, `alg=none`, отсутствие каждого claim, неверные типы claim, несуществующий и деактивированный пользователь, устаревшая роль, версия токена (меньше/больше/отрицательная); `require_roles`: 401 против 403, у admin нет неявного доступа, `require_roles()` без ролей |
| test_gen_users.py: список | порядок, фильтры role/is_active/комбинация, поиск по кириллице и email без учёта регистра, `%`/`_` как литералы, пустой `q`, пагинация, лимит по умолчанию, границы и 422 параметров |
| test_gen_users.py: создание | все роли, хэш пароля, вход сразу, нормализация, занятый email (регистр), 422 на лишние поля, роль, пароль, имя, обязательные поля |
| test_gen_users.py: изменение | поля и БД, пустое тело 422, null 422, 404, невалидный id, занятый email, свой email в другом регистре; самоблокировка admin (роль, деактивация, атомарность, допустимые правки, действия над другим admin); смена роли при профиле врача / пациента (409 с текстом) и без профиля; токен после смены роли, пароля (token_version, старый пароль, новый вход), деактивации, реактивации |
| test_gen_doctors.py | чтение (ФИО, деактивированный по id, 404, 422 id), список (сортировка, исключение неактивных, фильтр специальности без регистра, точное совпадение, границы длины); создание (тело и БД, значения по умолчанию, сортировка дней, 404/422/409, 23 невалидных тела, граничные принятые значения); изменение (частичное, график целиком, 404, null, одна граница против сохранённой, 409 с числом записей, границы `work_end`, дни, сетка слотов, лимит 4 слота, статусы scheduled/cancelled/completed/no_show, прошлое, `starts_at == now` и `now + 1 с`, чужой врач, неизменный график, подмена `get_now`) |
| test_gen_patients.py | список (порядок, поля, поиск ФИО по кириллице и полису, wildcard, пагинация, 422, 403 пациенту); видимость врачу по статусам записи, без дублей, без профиля; `/me`; чужой и несуществующий id неразличимы (404); создание (роли, привязка, 404/422/409, 23 невалидных тела, границы); изменение (поля, `phone: null`, 409 полиса); привязка и отвязка аккаунта, перепривязка, роль привязываемого, занятый аккаунт |
| test_gen_access_matrix.py | 19 эндпоинтов x 5 ролей = 95 проверок кода, а также `detail` и `WWW-Authenticate` |

## Найденные ошибки приложения

Не найдено: все проверенные поведения соответствуют коду и документации в докстрингах. Тесты с `xfail` не понадобились.

## Замечания

- Тесты берут `PASSWORD` через `from conftest import PASSWORD` (в `tests/` нет `__init__.py`).
- Проверка даты рождения в схемах использует реальные часы (`datetime.now`), а не `get_now`, поэтому тесты используют только заведомо безопасные даты (1900, 1985, 2999, 1800) и не проверяют границу «завтра» и граничный год.
- В `test_gen_security.py` HS512-токен даёт предупреждение PyJWT о длине ключа; на результат оно не влияет.
- Тесты на PostgreSQL не запускались (только SQLite).

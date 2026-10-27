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
    [
        {},
        {"Authorization": ""},
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer "},
        {"Authorization": "Basic dXNlcjpwYXNz"},
        {"Authorization": "Token abc"},
    ],
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

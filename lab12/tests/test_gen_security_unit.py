"""Unit-тесты app/security.py без HTTP: пароли (bcrypt), JWT, require_roles."""

import base64
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import jwt
import pytest
from fastapi import HTTPException

from app import security
from app.config import get_settings
from app.models import UserRole
from app.security import (
    JWT_ALGORITHM,
    TokenError,
    TokenPayload,
    create_access_token,
    decode_access_token,
    hash_password,
    hash_password_async,
    require_roles,
    verify_password,
    verify_password_async,
)

SECRET = get_settings().jwt_secret.get_secret_value()


@pytest.fixture(scope="module")
def pw_hash() -> str:
    """Один дорогой bcrypt-хэш на модуль для пароля 'Password123'."""
    return hash_password("Password123")


def exp_in(seconds: int = 3600) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=seconds)


def claims(**override):
    data = {"sub": "5", "role": "doctor", "ver": 0, "exp": exp_in()}
    data.update(override)
    return {k: v for k, v in data.items() if v is not ...}


def forge(payload: dict, key: str = SECRET, alg: str = JWT_ALGORITHM) -> str:
    return jwt.encode(payload, key, algorithm=alg)


def b64(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


# --------------------------------------------------------------------------- #
# hash_password / verify_password
# --------------------------------------------------------------------------- #
# длина в байтах UTF-8: 72 -> ok, 73 -> ValueError; пустой пароль; юникод;
# verify: верный / неверный / регистр / пробел / битый хэш / не-ASCII хэш / пароль > 72 байт.


def test_hash_password_format(pw_hash):
    assert pw_hash.startswith("$2b$12$")
    assert len(pw_hash) == 60
    assert pw_hash.isascii()


def test_hash_password_is_salted(pw_hash):
    assert hash_password("Password123") != pw_hash


def test_verify_password_correct(pw_hash):
    assert verify_password("Password123", pw_hash) is True


@pytest.mark.parametrize(
    "wrong",
    ["password123", "Password123 ", " Password123", "Password124", "", "Password12", "Password1234"],
    ids=["case", "trailing_space", "leading_space", "last_char", "empty", "prefix", "extra_char"],
)
def test_verify_password_wrong_password_false(pw_hash, wrong):
    assert verify_password(wrong, pw_hash) is False


@pytest.mark.parametrize(
    "password",
    ["a" * 72, "я" * 36, "😀" * 18, "пароль-Ж-123", ""],
    ids=["72_ascii_bytes", "72_cyrillic_bytes", "72_emoji_bytes", "unicode", "empty"],
)
def test_hash_password_roundtrip_including_limit_and_unicode(password):
    hashed = hash_password(password)
    assert verify_password(password, hashed) is True
    if len(password.encode()) < 72:
        assert verify_password(password + "x", hashed) is False


@pytest.mark.parametrize(
    "password",
    ["a" * 73, "я" * 37, "a" * 71 + "я", "😀" * 19, "a" * 1000],
    ids=["73_bytes", "74_bytes_cyrillic", "73_bytes_mixed", "76_bytes_emoji", "1000"],
)
def test_hash_password_over_72_bytes_raises(password):
    with pytest.raises(ValueError, match="72 bytes"):
        hash_password(password)


def test_verify_password_differs_in_last_byte_of_72_bytes():
    hashed = hash_password("a" * 72)
    assert verify_password("a" * 71 + "b", hashed) is False


def test_verify_password_over_72_bytes_is_false_not_truncated_match():
    hashed = hash_password("a" * 72)
    assert verify_password("a" * 73, hashed) is False
    assert verify_password("a" * 500, hashed) is False


@pytest.mark.parametrize(
    "bad_hash",
    [
        "",
        "not-a-hash",
        "$2b$12$short",
        "$2b$12$" + "x" * 53,
        "$1$salt$abcdefghijklmnopqrstuv",
        "хэш",
        "$2b$99$" + "a" * 53,
    ],
    ids=["empty", "garbage", "truncated", "invalid_chars_body", "md5_crypt", "non_ascii", "bad_rounds"],
)
def test_verify_password_malformed_hash_returns_false(bad_hash):
    assert verify_password("Password123", bad_hash) is False


def test_verify_password_does_not_accept_hash_as_password(pw_hash):
    assert verify_password(pw_hash, pw_hash) is False


async def test_async_wrappers_roundtrip(pw_hash):
    hashed = await hash_password_async("Password123")
    assert hashed.startswith("$2b$12$")
    assert await verify_password_async("Password123", hashed) is True
    assert await verify_password_async("wrong", hashed) is False
    assert await verify_password_async("Password123", pw_hash) is True


async def test_hash_password_async_over_limit_raises():
    with pytest.raises(ValueError, match="72 bytes"):
        await hash_password_async("a" * 73)


async def test_verify_password_async_malformed_hash_false():
    assert await verify_password_async("x", "broken") is False


# --------------------------------------------------------------------------- #
# create_access_token / decode_access_token: корректные токены
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("role", list(UserRole), ids=lambda r: r.value)
def test_token_roundtrip_for_every_role(role):
    payload = decode_access_token(create_access_token(42, role, version=3))
    assert isinstance(payload, TokenPayload)
    assert (payload.user_id, payload.role, payload.version) == (42, role, 3)


@pytest.mark.parametrize(("user_id", "version"), [(1, 0), (2_147_483_647, 0), (1, 1), (7, 2_147_483_647)])
def test_token_roundtrip_boundary_ids_and_versions(user_id, version):
    payload = decode_access_token(create_access_token(user_id, UserRole.ADMIN, version=version))
    assert (payload.user_id, payload.version) == (user_id, version)


def test_token_raw_claims_and_header():
    token = create_access_token(42, UserRole.DOCTOR, version=3)
    raw = jwt.decode(token, SECRET, algorithms=[JWT_ALGORITHM])
    assert raw["sub"] == "42"
    assert raw["role"] == "doctor"
    assert raw["ver"] == 3
    assert isinstance(raw["exp"], int)
    assert set(raw) == {"sub", "role", "ver", "exp"}
    assert jwt.get_unverified_header(token)["alg"] == "HS256"


def test_token_default_ttl_comes_from_settings():
    before = datetime.now(UTC)
    payload = decode_access_token(create_access_token(1, UserRole.PATIENT, version=0))
    expected = before + timedelta(minutes=get_settings().jwt_ttl_minutes)
    assert expected - timedelta(seconds=2) <= payload.expires_at <= expected + timedelta(seconds=2)
    assert payload.expires_at.utcoffset() == timedelta(0)


@pytest.mark.parametrize("delta", [timedelta(seconds=30), timedelta(days=7)], ids=["30s", "7d"])
def test_token_custom_ttl_is_applied(delta):
    before = datetime.now(UTC)
    payload = decode_access_token(create_access_token(1, UserRole.PATIENT, version=0, expires_delta=delta))
    assert before + delta - timedelta(seconds=2) <= payload.expires_at <= before + delta + timedelta(seconds=2)


def test_token_zero_ttl_is_not_replaced_by_default():
    # timedelta(0) - ложное значение, но это явный срок: токен сразу просрочен
    with pytest.raises(TokenError):
        decode_access_token(create_access_token(1, UserRole.PATIENT, version=0, expires_delta=timedelta(0)))


@pytest.mark.parametrize(
    "delta", [timedelta(seconds=-1), timedelta(seconds=-3600), timedelta(days=-365)], ids=["1s", "1h", "1y"]
)
def test_expired_token_raises(delta):
    token = create_access_token(1, UserRole.ADMIN, version=0, expires_delta=delta)
    with pytest.raises(TokenError, match="invalid token"):
        decode_access_token(token)


def test_token_valid_for_next_minute_and_extra_claims_are_ignored():
    token = forge(claims(exp=exp_in(60), extra="x", iat=int(datetime.now(UTC).timestamp()) - 5))
    assert decode_access_token(token).user_id == 5


def test_token_error_has_cause_from_library():
    with pytest.raises(TokenError) as exc:
        decode_access_token(create_access_token(1, UserRole.ADMIN, version=0, expires_delta=timedelta(seconds=-1)))
    assert isinstance(exc.value.__cause__, jwt.ExpiredSignatureError)


# --------------------------------------------------------------------------- #
# decode_access_token: подделки и некорректные токены
# --------------------------------------------------------------------------- #


def test_token_signed_with_other_key_rejected():
    with pytest.raises(TokenError):
        decode_access_token(forge(claims(), key="another-secret-0123456789abcdef0123"))


def test_token_signed_with_key_differing_in_one_char_rejected():
    with pytest.raises(TokenError):
        decode_access_token(forge(claims(), key=SECRET[:-1] + ("x" if SECRET[-1] != "x" else "y")))


def test_token_alg_none_rejected():
    with pytest.raises(TokenError):
        decode_access_token(jwt.encode(claims(), None, algorithm="none"))


def test_token_alg_none_handmade_rejected():
    token = f"{b64({'alg': 'none', 'typ': 'JWT'})}.{b64({'sub': '1', 'role': 'admin', 'ver': 0, 'exp': 4102444800})}."
    with pytest.raises(TokenError):
        decode_access_token(token)


def test_token_with_stripped_signature_rejected():
    token = create_access_token(1, UserRole.ADMIN, version=0)
    with pytest.raises(TokenError):
        decode_access_token(token.rsplit(".", 1)[0] + ".")
    with pytest.raises(TokenError):
        decode_access_token(token.rsplit(".", 1)[0])


@pytest.mark.parametrize("alg", ["HS384", "HS512"])
def test_token_other_hmac_algorithm_with_correct_key_rejected(alg):
    with pytest.raises(TokenError):
        decode_access_token(forge(claims(), alg=alg))


def test_token_with_tampered_payload_rejected():
    token = create_access_token(5, UserRole.PATIENT, version=0)
    header, _, signature = token.split(".")
    evil = b64({"sub": "5", "role": "admin", "ver": 0, "exp": 4102444800})
    with pytest.raises(TokenError):
        decode_access_token(f"{header}.{evil}.{signature}")


def test_token_with_flipped_signature_character_rejected():
    token = create_access_token(5, UserRole.PATIENT, version=0)
    head, signature = token[:-4], token[-4:]
    flipped = head + ("AAAA" if signature != "AAAA" else "BBBB")
    with pytest.raises(TokenError):
        decode_access_token(flipped)


@pytest.mark.parametrize("missing", ["sub", "role", "ver", "exp"])
def test_token_without_required_claim_rejected(missing):
    with pytest.raises(TokenError):
        decode_access_token(forge(claims(**{missing: ...})))


@pytest.mark.parametrize(
    "sub", ["abc", "", "1.5", "5x", " ", "null"], ids=["text", "empty", "float", "suffix", "blank", "null_str"]
)
def test_token_with_non_numeric_sub_rejected(sub):
    with pytest.raises(TokenError):
        decode_access_token(forge(claims(sub=sub)))


@pytest.mark.parametrize("sub", [5, None, ["5"]], ids=["int", "none", "list"])
def test_token_with_non_string_sub_rejected(sub):
    with pytest.raises(TokenError):
        decode_access_token(forge(claims(sub=sub)))


@pytest.mark.parametrize(
    "role", ["superuser", "ADMIN", "", None, 5, ["admin"]], ids=["unknown", "uppercase", "empty", "none", "int", "list"]
)
def test_token_with_invalid_role_rejected(role):
    with pytest.raises(TokenError):
        decode_access_token(forge(claims(role=role)))


@pytest.mark.parametrize(
    "ver",
    ["1", "0", 1.0, 1.5, True, False, None, [1], {"v": 1}],
    ids=["str_1", "str_0", "float_1", "float_1.5", "true", "false", "none", "list", "dict"],
)
def test_token_with_non_int_ver_rejected(ver):
    with pytest.raises(TokenError):
        decode_access_token(forge(claims(ver=ver)))


@pytest.mark.parametrize(
    "exp", ["tomorrow", "9999999999", None, [1], True], ids=["text", "numeric_str", "none", "list", "bool"]
)
def test_token_with_invalid_exp_rejected(exp):
    with pytest.raises(TokenError):
        decode_access_token(forge(claims(exp=exp)))


def test_token_not_yet_valid_nbf_rejected():
    with pytest.raises(TokenError):
        decode_access_token(forge(claims(nbf=int((datetime.now(UTC) + timedelta(hours=1)).timestamp()))))


def test_token_with_unexpected_audience_rejected():
    with pytest.raises(TokenError):
        decode_access_token(forge(claims(aud="someone-else")))


@pytest.mark.parametrize(
    "token",
    ["", " ", "abc", "a.b", "a.b.c", "...", "Bearer x.y.z", "eyJhbGciOiJIUzI1NiJ9..", "\x00", "тест.тест.тест"],
    ids=[
        "empty",
        "blank",
        "one_part",
        "two_parts",
        "garbage_three_parts",
        "dots_only",
        "bearer_prefix",
        "empty_payload",
        "nul",
        "cyrillic",
    ],
)
def test_garbage_token_rejected(token):
    with pytest.raises(TokenError):
        decode_access_token(token)


def test_token_with_surrounding_whitespace_rejected():
    token = create_access_token(1, UserRole.ADMIN, version=0)
    with pytest.raises(TokenError):
        decode_access_token(f" {token}")


def test_token_error_is_not_leaking_library_exceptions():
    for token in ("", "a.b.c", forge(claims(ver="1"))):
        with pytest.raises(TokenError):
            decode_access_token(token)


def test_module_constants():
    assert JWT_ALGORITHM == "HS256"
    assert security.BCRYPT_ROUNDS == 12


# --------------------------------------------------------------------------- #
# require_roles (без HTTP: зависимость вызывается напрямую)
# --------------------------------------------------------------------------- #


def test_require_roles_without_roles_raises():
    with pytest.raises(ValueError, match="at least one role"):
        require_roles()


@pytest.mark.parametrize(
    ("allowed", "actual"),
    [
        ((UserRole.ADMIN,), UserRole.ADMIN),
        ((UserRole.ADMIN, UserRole.REGISTRAR), UserRole.REGISTRAR),
        ((UserRole.DOCTOR, UserRole.PATIENT), UserRole.PATIENT),
        (tuple(UserRole), UserRole.DOCTOR),
    ],
    ids=["single", "second_of_two", "patient", "all_roles"],
)
async def test_require_roles_allows_listed_role(allowed, actual):
    user = SimpleNamespace(role=actual)
    assert await require_roles(*allowed)(user=user) is user


@pytest.mark.parametrize(
    ("allowed", "actual"),
    [
        ((UserRole.DOCTOR,), UserRole.ADMIN),
        ((UserRole.ADMIN,), UserRole.REGISTRAR),
        ((UserRole.ADMIN, UserRole.REGISTRAR), UserRole.PATIENT),
        ((UserRole.PATIENT,), UserRole.DOCTOR),
    ],
    ids=["admin_not_implicitly_allowed", "registrar_vs_admin", "patient_vs_staff", "doctor_vs_patient"],
)
async def test_require_roles_rejects_other_role_with_403(allowed, actual):
    with pytest.raises(HTTPException) as exc:
        await require_roles(*allowed)(user=SimpleNamespace(role=actual))
    assert exc.value.status_code == 403
    assert exc.value.detail == "Insufficient permissions"

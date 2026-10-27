"""Проверка JWT в Python по JWKS Go-сервиса (повышенное задание №3)."""

from __future__ import annotations

import base64
import time
from typing import Any

import httpx
import jwt
import pytest
from conftest import AUDIENCE, ISSUER, FakeGo, SigningKey, new_key
from fastapi.testclient import TestClient

from app.auth import JWKSUnavailableError, JWKSVerifier, TokenError


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_me_with_valid_token(client: TestClient, key: SigningKey) -> None:
    r = client.get("/api/v1/me", headers=auth(key.token(sub="alice", role="admin")))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["username"] == "alice"
    assert body["role"] == "admin"
    assert body["verified_by"] == "python:jwks"


def test_missing_token(client: TestClient) -> None:
    r = client.get("/api/v1/me")
    assert r.status_code == 401
    assert r.headers["www-authenticate"].startswith("Bearer")
    r = client.get("/api/v1/me", headers={"Authorization": "Basic abc"})
    assert r.status_code == 401


def test_rejected_tokens(client: TestClient, key: SigningKey) -> None:
    other = new_key(key.kid)  # тот же kid, но другой ключ — подделка
    now = int(time.time())
    public_n = base64.b64encode(key.private.public_key().public_numbers().n.to_bytes(256, "big"))
    cases = {
        "истёк": key.token(exp=now - 3600, iat=now - 7200),
        "чужой audience": key.token(aud=["other"]),
        "чужой issuer": key.token(iss="evil"),
        "без exp": key.token(exp=None),
        "без sub": key.token(sub=None),
        "чужая подпись": other.token(),
        "alg=none": jwt.encode({"sub": "alice"}, None, algorithm="none", headers={"kid": key.kid}),
        "HS256 с открытым ключом": jwt.encode({"sub": "alice"}, public_n, algorithm="HS256", headers={"kid": key.kid}),
        "мусор": "not.a.jwt",
    }
    for name, token in cases.items():
        r = client.get("/api/v1/me", headers=auth(token))
        assert r.status_code == 401, name
        assert 'error="invalid_token"' in r.headers["www-authenticate"], name


def test_jwks_cached(client: TestClient, key: SigningKey, fake_go: FakeGo) -> None:
    for _ in range(5):
        assert client.get("/api/v1/me", headers=auth(key.token())).status_code == 200
    assert fake_go.jwks_calls == 1


def test_jwks_unavailable(client: TestClient, key: SigningKey, fake_go: FakeGo) -> None:
    fake_go.jwks_down = True
    r = client.get("/api/v1/me", headers=auth(key.token()))
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "auth_unavailable"


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def test_stale_key_used_when_go_unavailable(key: SigningKey) -> None:
    fake = FakeGo(keys=[key])
    clock = _Clock()
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handler), base_url="http://go") as http:
        v = JWKSVerifier(http, "http://go/.well-known/jwks.json", ISSUER, AUDIENCE, cache_ttl=300, clock=clock)
        await v.verify(key.token())
        assert fake.jwks_calls == 1

        # TTL истёк, а Go лежит: токен с известным kid всё равно принимается.
        fake.jwks_down = True
        clock.now += 301
        assert (await v.verify(key.token()))["sub"] == "alice"
        assert fake.jwks_calls == 2

        # Следующая попытка обновления — не раньше чем через MIN_REFRESH_INTERVAL.
        await v.verify(key.token())
        assert fake.jwks_calls == 2
        clock.now += 31
        await v.verify(key.token())
        assert fake.jwks_calls == 3

        # Неизвестный kid при недоступном Go — 503, а не 401: токен может быть валидным.
        clock.now += 31
        with pytest.raises(JWKSUnavailableError):
            await v.verify(new_key("other-kid").token())


@pytest.mark.parametrize("payload", [[], {"keys": "oops"}, "not json", {"keys": [1, None, {"kty": "RSA"}]}])
async def test_malformed_jwks(key: SigningKey, payload: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(payload, str):
            return httpx.Response(200, text=payload)
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        v = JWKSVerifier(http, "http://go/.well-known/jwks.json", ISSUER, AUDIENCE)
        # Некорректный JWKS — понятная ошибка (503 или 401 «неизвестный kid»), а не 500.
        with pytest.raises((JWKSUnavailableError, TokenError)):
            await v.verify(key.token())


async def test_key_rotation_and_refresh_limit(key: SigningKey) -> None:
    rotated = new_key("test-kid-2")
    fake = FakeGo(keys=[key])
    clock = _Clock()
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handler), base_url="http://go") as http:
        v = JWKSVerifier(http, "http://go/.well-known/jwks.json", ISSUER, AUDIENCE, cache_ttl=300, clock=clock)
        assert (await v.verify(key.token()))["sub"] == "alice"
        assert fake.jwks_calls == 1

        # Неизвестный kid сразу после загрузки — не дёргаем Go повторно (защита от DoS).
        with pytest.raises(TokenError, match="неизвестный kid"):
            await v.verify(rotated.token())
        assert fake.jwks_calls == 1

        # Go сменил ключ; через 30+ с неизвестный kid вызывает обновление JWKS.
        fake.keys = [key, rotated]
        clock.now += 31
        assert (await v.verify(rotated.token()))["sub"] == "alice"
        assert fake.jwks_calls == 2

        # По истечении TTL кеш обновляется даже для известного kid.
        clock.now += 301
        await v.verify(key.token())
        assert fake.jwks_calls == 3

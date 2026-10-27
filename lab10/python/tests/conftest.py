"""Общие фикстуры: RSA-ключи, выпуск токенов и поддельный Go-сервис на httpx.MockTransport."""

from __future__ import annotations

import base64
import datetime as dt
import json
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

ISSUER = "lab10-go-api"
AUDIENCE = "lab10"


def _b64(n: int) -> str:
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


@dataclass
class SigningKey:
    kid: str
    private: rsa.RSAPrivateKey

    def jwk(self) -> dict[str, str]:
        pub = self.private.public_key().public_numbers()
        return {"kty": "RSA", "use": "sig", "alg": "RS256", "kid": self.kid, "n": _b64(pub.n), "e": _b64(pub.e)}

    def token(self, sub: str = "alice", role: str = "user", **overrides: Any) -> str:
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": ISSUER,
            "aud": [AUDIENCE],
            "sub": sub,
            "role": role,
            "iat": now,
            "nbf": now,
            "exp": now + 300,
            "jti": str(uuid.uuid4()),
        }
        claims.update(overrides)
        claims = {k: v for k, v in claims.items() if v is not None}
        return jwt.encode(claims, self.private, algorithm="RS256", headers={"kid": self.kid})


def new_key(kid: str) -> SigningKey:
    return SigningKey(kid, rsa.generate_private_key(public_exponent=65537, key_size=2048))


@pytest.fixture(scope="session")
def key() -> SigningKey:
    return new_key("test-kid-1")


def go_order(owner: str = "alice", created_at: str = "2026-10-09T12:00:00Z", **request: Any) -> dict[str, Any]:
    """Заказ в том виде, в каком его возвращает Go (см. go/internal/orders/model.go)."""
    body = {**valid_order(), **request}
    cents = sum(round(i["price"] * 100) * i["quantity"] for i in body["items"])
    return {
        "id": str(uuid.uuid4()),
        "owner": owner,
        "customer": {"phone": "", **body["customer"]},
        "items": body["items"],
        "delivery": body["delivery"],
        "comment": body.get("comment", ""),
        "total_cents": cents,
        "total": cents / 100,
        "created_at": created_at,
    }


def _response(status: int, body: Any) -> httpx.Response:
    """Ответ поддельного Go: dict — JSON, str — «чужое» тело (HTML прокси и т. п.)."""
    headers = {"WWW-Authenticate": 'Bearer realm="lab10", error="invalid_token"'} if status == 401 else {}
    if isinstance(body, str):
        return httpx.Response(status, text=body, headers={**headers, "Content-Type": "text/html"})
    return httpx.Response(status, json=body, headers=headers)


@dataclass
class FakeGo:
    """Поддельный Go-сервис: JWKS и заказы. Запоминает запросы для проверок."""

    keys: list[SigningKey]
    orders: list[dict[str, Any]] = field(default_factory=list)
    jwks_calls: int = 0
    jwks_down: bool = False
    orders_status: int = 200
    # Если задано — POST /api/v1/orders вернёт это тело вместо созданного заказа.
    create_response: tuple[int, dict[str, Any] | str] | None = None
    seen_headers: list[httpx.Headers] = field(default_factory=list)
    received: list[dict[str, Any]] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/jwks.json":
            self.jwks_calls += 1
            if self.jwks_down:
                raise httpx.ConnectError("connection refused", request=request)
            return httpx.Response(200, json={"keys": [k.jwk() for k in self.keys]})
        if request.url.path == "/api/v1/orders":
            self.seen_headers.append(request.headers)
            if request.method == "POST":
                body = json.loads(request.content)
                self.received.append(body)
                if self.create_response is not None:
                    return _response(*self.create_response)
                order = go_order(**body)
                self.orders.append(order)
                return httpx.Response(201, json=order, headers={"Location": f"/api/v1/orders/{order['id']}"})
            if self.orders_status != 200:
                return _response(self.orders_status, {"error": {"code": "x", "message": "x"}})
            return httpx.Response(200, json={"orders": self.orders, "count": len(self.orders)})
        return httpx.Response(404)


@pytest.fixture
def fake_go(key: SigningKey) -> FakeGo:
    return FakeGo(keys=[key])


@pytest.fixture
def settings() -> Settings:
    return Settings(go_api_url="http://go-api.test", jwt_issuer=ISSUER, jwt_audience=AUDIENCE)


@pytest.fixture
def client(settings: Settings, fake_go: FakeGo) -> Iterator[TestClient]:
    app = create_app(settings, transport=httpx.MockTransport(fake_go.handler))
    with TestClient(app) as c:  # контекст запускает lifespan
        yield c


def valid_order(**overrides: Any) -> dict[str, Any]:
    order: dict[str, Any] = {
        "customer": {"name": "Иван Петров", "email": "ivan@example.com", "phone": "+79991234567"},
        "items": [
            {"sku": "ABC-123", "quantity": 2, "price": 199.99},
            {"sku": "DEF-4567", "quantity": 1, "price": 50},
        ],
        "delivery": {
            "address": "Москва, ул. Пушкина, 1",
            "date": (dt.datetime.now(dt.UTC).date() + dt.timedelta(days=3)).isoformat(),
        },
    }
    order.update(overrides)
    return order

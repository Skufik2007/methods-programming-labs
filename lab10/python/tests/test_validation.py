"""Валидация заказа в FastAPI: правила и коды ответов совпадают с Go-сервисом."""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterator
from typing import Any

import pytest
from conftest import valid_order
from fastapi.testclient import TestClient

from app import models

URL = "/api/v1/orders/validate"


def test_valid_order(client: TestClient) -> None:
    r = client.post(URL, json=valid_order())
    assert r.status_code == 200, r.text
    assert r.json() == {"valid": True, "items": 2, "total_cents": 44998, "total": 449.98}


def _mutate(path: str, value: Any) -> dict[str, Any]:
    order = valid_order()
    target: Any = order
    keys = path.split(".")
    for k in keys[:-1]:
        target = target[int(k)] if k.isdigit() else target[k]
    last = keys[-1]
    if value is ...:
        del target[last]
    elif last.isdigit():
        target[int(last)] = value
    else:
        target[last] = value
    return order


# Те же случаи, что в go/internal/orders/validation_test.go.
@pytest.mark.parametrize(
    ("path", "value", "field", "rule"),
    [
        ("items", [], "items", "min"),
        ("customer.name", "И", "customer.name", "min"),
        ("customer.email", "ivan@", "customer.email", "email"),
        ("customer.email", "ivan@ex..com", "customer.email", "email"),
        ("customer.email", "ivan@localhost", "customer.email", "email"),
        ("customer.name", "    ", "customer.name", "notblank"),
        ("delivery.address", "\t      ", "delivery.address", "notblank"),
        ("items.0.price", 1_000_000.01, "items[0].price", "max"),
        ("customer.phone", "8-999-123", "customer.phone", "e164"),
        ("items.0.sku", "abc-123", "items[0].sku", "sku"),
        ("items.1.sku", "ABC-", "items[1].sku", "sku"),
        ("items.0.quantity", 0, "items[0].quantity", "min"),
        ("items.0.quantity", 101, "items[0].quantity", "max"),
        ("items.0.price", -1, "items[0].price", "gt"),
        ("items.0.price", 1.999, "items[0].price", "money"),
        ("delivery.date", "2020-01-01", "delivery.date", "delivery_date"),
        ("items.1.sku", "ABC-123", "items[1].sku", "unique_sku"),
        ("comment", "x" * 501, "comment", "max"),
        ("customer.name", ..., "customer.name", "required"),
    ],
)
def test_rules(client: TestClient, path: str, value: Any, field: str, rule: str) -> None:
    r = client.post(URL, json=_mutate(path, value))
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "validation_failed"
    assert {"field": field, "rule": rule} in [{"field": d["field"], "rule": d["rule"]} for d in err["details"]]
    assert all(d["message"] for d in err["details"])


@pytest.mark.parametrize(
    ("body", "status", "code"),
    [
        ("", 400, "empty_body"),
        ('{"customer":', 400, "invalid_json"),
        ('{"items": "many"}', 400, "invalid_type"),
        ('{"customer": {"name": "Иван", "email": "a@b.c"}, "hack": 1}', 400, "unknown_field"),
    ],
)
def test_bad_requests(client: TestClient, body: str, status: int, code: str) -> None:
    r = client.post(URL, content=body, headers={"Content-Type": "application/json"})
    assert r.status_code == status, r.text
    assert r.json()["error"]["code"] == code


def test_empty_phone_is_absent(client: TestClient) -> None:
    # Как omitempty в Go: пустой телефон — «не указан», а не ошибка формата.
    r = client.post(URL, json=_mutate("customer.phone", ""))
    assert r.status_code == 200, r.text


def test_strict_types(client: TestClient) -> None:
    # "2" как строка не превращается молча в число — как в Go.
    r = client.post(URL, json=_mutate("items.0.quantity", "2"))
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_type"


def test_body_too_large(client: TestClient) -> None:
    r = client.post(URL, json=valid_order(comment="x" * (1 << 20)))
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "body_too_large"


def test_body_too_large_without_content_length(client: TestClient) -> None:
    # Тело передаётся частями (chunked), размер заранее неизвестен.
    def chunks() -> Iterator[bytes]:
        yield b'{"comment": "'
        for _ in range(300):
            yield b"x" * 4096
        yield b'"}'

    r = client.post(URL, content=chunks(), headers={"Content-Type": "application/json"})
    assert r.status_code == 413, r.text
    assert r.json()["error"]["code"] == "body_too_large"


def test_chunked_body_under_limit(client: TestClient) -> None:
    body = json.dumps(valid_order()).encode()
    r = client.post(URL, content=iter([body[:10], body[10:]]), headers={"Content-Type": "application/json"})
    assert r.status_code == 200, r.text


def test_delivery_window(monkeypatch: pytest.MonkeyPatch) -> None:
    # Границы как в TestDeliveryDateBoundaries (Go): сегодня и +90 дней — можно, +91 и вчера — нельзя.
    monkeypatch.setattr(models, "today", lambda: dt.date(2026, 10, 9))
    for date, valid in [("2026-10-09", True), ("2027-01-07", True), ("2027-01-08", False), ("2026-10-08", False)]:
        try:
            models.Delivery(address="Москва, ул. 1", date=dt.date.fromisoformat(date))
        except ValueError:
            assert not valid, date
        else:
            assert valid, date


def test_total_cents_exact() -> None:
    order = models.OrderRequest.model_validate(
        valid_order(
            items=[
                {"sku": "AAA-001", "quantity": 1, "price": 0.1},
                {"sku": "AAA-002", "quantity": 1, "price": 0.2},
                {"sku": "AAA-003", "quantity": 3, "price": 199.99},
            ]
        )
    )
    assert order.total_cents() == 60027  # как TestTotalCents в Go


def test_service_endpoints(client: TestClient) -> None:
    assert client.get("/ping").json() == {"message": "pong"}
    assert client.get("/health/live").status_code == 200
    assert client.get("/health/ready").status_code == 200
    r = client.get("/ping", headers={"X-Request-ID": "abc-123"})
    assert r.headers["x-request-id"] == "abc-123"
    r = client.get("/ping", headers={"X-Request-ID": "<script>"})
    assert r.headers["x-request-id"] != "<script>"
    assert client.get("/nope").json()["error"]["code"] == "http_error"
    assert client.get("/openapi.json").json()["info"]["title"] == "Lab10 Python service"

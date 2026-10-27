"""Сложные структуры JSON между Python и Go (средней сложности №5).

Python → Go: вложенный заказ уходит в Go в виде JSON (даты строками, без null-полей).
Go → Python: ответы разбираются по типизированному контракту (app/contract.py),
нарушение контракта даёт 502, а не 500.
"""

from __future__ import annotations

from conftest import FakeGo, SigningKey, go_order, valid_order
from fastapi.testclient import TestClient


def auth(key: SigningKey, **headers: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key.token(sub='alice')}", **headers}


def test_create_order_sends_nested_json(client: TestClient, key: SigningKey, fake_go: FakeGo) -> None:
    order = valid_order(comment="Позвонить за час — домофон «15К»")
    del order["customer"]["phone"]

    r = client.post("/api/v1/orders", json=order, headers=auth(key, **{"X-Request-ID": "req-7"}))
    assert r.status_code == 201, r.text

    # Что получил Go: та же вложенная структура, дата строкой, без "phone": null.
    sent = fake_go.received[-1]
    assert sent == order
    assert "phone" not in sent["customer"]
    assert fake_go.seen_headers[-1]["x-request-id"] == "req-7"

    # Что вернул Python: ответ Go, разобранный по контракту и сериализованный обратно.
    body = r.json()
    assert body["items"] == order["items"]
    assert body["delivery"] == order["delivery"]
    assert body["comment"] == order["comment"]  # юникод не искажается
    assert body["customer"]["phone"] == ""
    assert body["total_cents"] == 44998
    assert r.headers["location"] == f"/api/v1/orders/{body['id']}"


def test_create_order_validated_in_python_first(client: TestClient, key: SigningKey, fake_go: FakeGo) -> None:
    bad = valid_order(items=[{"sku": "bad", "quantity": 1, "price": 1}])
    r = client.post("/api/v1/orders", json=bad, headers=auth(key))
    assert r.status_code == 422
    assert fake_go.received == []  # до Go некорректный заказ не доходит


def test_create_order_passes_go_errors(client: TestClient, key: SigningKey, fake_go: FakeGo) -> None:
    go_error = {"error": {"code": "validation_failed", "message": "x", "details": [{"field": "a", "rule": "b"}]}}
    fake_go.create_response = (422, go_error)
    r = client.post("/api/v1/orders", json=valid_order(), headers=auth(key))
    assert r.status_code == 422
    assert r.json() == go_error


def test_go_auth_errors_passed_through(client: TestClient, key: SigningKey, fake_go: FakeGo) -> None:
    # Токен прошёл проверку в Python, но Go его отклонил (например, истёк между запросами):
    # в обоих эндпоинтах клиент получает 401 от Go, а не 502.
    fake_go.orders_status = 401
    r = client.get("/api/v1/orders/summary", headers=auth(key))
    assert r.status_code == 401
    assert r.headers["www-authenticate"].startswith("Bearer")

    fake_go.create_response = (401, {"error": {"code": "invalid_token", "message": "x"}})
    r = client.post("/api/v1/orders", json=valid_order(), headers=auth(key))
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "invalid_token"


def test_go_error_not_in_common_format_is_502(client: TestClient, key: SigningKey, fake_go: FakeGo) -> None:
    # Раньше resp.json() на HTML-теле давал 500.
    fake_go.create_response = (422, "<html>bad gateway page</html>")
    r = client.post("/api/v1/orders", json=valid_order(), headers=auth(key))
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "upstream_error"

    fake_go.create_response = (403, {"unexpected": "shape"})
    assert client.post("/api/v1/orders", json=valid_order(), headers=auth(key)).status_code == 502


def test_contract_violation_is_502(client: TestClient, key: SigningKey, fake_go: FakeGo) -> None:
    broken = go_order()
    broken["total_cents"] += 1  # сумма не сходится с позициями
    fake_go.create_response = (201, broken)
    r = client.post("/api/v1/orders", json=valid_order(), headers=auth(key))
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "upstream_contract_violation"

    missing = go_order()
    del missing["delivery"]
    fake_go.orders = [missing]
    r = client.get("/api/v1/orders/summary", headers=auth(key))
    assert r.status_code == 502
    assert "delivery" in r.json()["error"]["message"]


def test_summary_aggregates_nested_items(client: TestClient, key: SigningKey, fake_go: FakeGo) -> None:
    fake_go.orders = [
        go_order(created_at="2026-10-09T10:00:00Z"),  # ABC-123 ×2 по 199.99, DEF-4567 ×1 по 50
        go_order(
            created_at="2026-10-09T11:00:00Z",
            items=[{"sku": "ABC-123", "quantity": 1, "price": 199.99}, {"sku": "XYZ-001", "quantity": 4, "price": 0.5}],
        ),
    ]
    r = client.get("/api/v1/orders/summary", headers=auth(key, **{"X-Request-ID": "req-42"}))
    assert r.status_code == 200, r.text
    s = r.json()
    assert (s["username"], s["count"], s["items"], s["total_cents"]) == ("alice", 2, 4, 44998 + 19999 + 200)
    assert s["by_sku"] == [
        {"sku": "ABC-123", "quantity": 3, "total_cents": 59997},
        {"sku": "DEF-4567", "quantity": 1, "total_cents": 5000},
        {"sku": "XYZ-001", "quantity": 4, "total_cents": 200},
    ]
    assert s["last_order"]["id"] == fake_go.orders[1]["id"]  # самый поздний по created_at
    assert fake_go.seen_headers[-1]["authorization"].startswith("Bearer ")
    assert fake_go.seen_headers[-1]["x-request-id"] == "req-42"


def test_summary_empty(client: TestClient, key: SigningKey) -> None:
    s = client.get("/api/v1/orders/summary", headers=auth(key)).json()
    assert (s["count"], s["by_sku"], s["last_order"]) == (0, [], None)


def test_summary_upstream_errors(client: TestClient, key: SigningKey, fake_go: FakeGo) -> None:
    fake_go.orders_status = 500
    r = client.get("/api/v1/orders/summary", headers=auth(key))
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "upstream_error"


def test_requires_token(client: TestClient, fake_go: FakeGo) -> None:
    assert client.get("/api/v1/orders/summary").status_code == 401
    assert client.post("/api/v1/orders", json=valid_order()).status_code == 401
    assert fake_go.seen_headers == []  # без токена до Go даже не доходим

"""Проверка развёрнутого docker compose (только стандартная библиотека).

    python deploy/smoke_test.py [--go http://localhost:8080] [--py http://localhost:8000]

Сценарий: вход в Go -> проверка токена в Python -> заказ в Go -> сводка в Python,
которую Python получает, обращаясь к Go по имени go-api внутри общей сети.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import urllib.error
import urllib.request
from typing import Any


def call(method: str, url: str, body: Any = None, token: str | None = None) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


def check(cond: bool, message: str) -> None:
    print(("OK   " if cond else "FAIL ") + message)
    if not cond:
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--go", default="http://localhost:8080")
    parser.add_argument("--py", default="http://localhost:8000")
    parser.add_argument("--user", default="student")
    parser.add_argument("--password", default="student-pass-1")
    args = parser.parse_args()

    status, body = call("POST", f"{args.go}/auth/login", {"username": args.user, "password": args.password})
    check(status == 200, f"Go: вход пользователя ({status})")
    token = body["access_token"]

    status, body = call("GET", f"{args.py}/api/v1/me", token=token)
    check(status == 200 and body.get("verified_by") == "python:jwks", f"Python проверил токен Go по JWKS ({status})")

    status, _ = call("GET", f"{args.py}/api/v1/me", token=token[:-4] + "AAAA")
    check(status == 401, f"Python отклонил подделанный токен ({status})")

    date = (dt.datetime.now(dt.UTC).date() + dt.timedelta(days=2)).isoformat()
    order = {
        "customer": {"name": "Иван Петров", "email": "ivan@example.com"},
        "items": [{"sku": "ABC-123", "quantity": 2, "price": 199.99}],
        "delivery": {"address": "Москва, ул. Пушкина, 1", "date": date},
    }
    status, _ = call("POST", f"{args.go}/api/v1/orders", order, token=token)
    check(status == 201, f"Go: заказ создан ({status})")

    via_py = {**order, "items": [{"sku": "XYZ-777", "quantity": 3, "price": 0.1}], "comment": "через Python «ок»"}
    status, created = call("POST", f"{args.py}/api/v1/orders", via_py, token=token)
    check(status == 201 and created["items"] == via_py["items"], f"Python передал вложенный заказ в Go ({status})")

    status, body = call("GET", f"{args.py}/api/v1/orders/summary", token=token)
    skus = {s["sku"]: s for s in body.get("by_sku", [])}
    check(
        status == 200 and body["count"] >= 2 and skus.get("XYZ-777", {}).get("total_cents") == 30,
        f"Python получил заказы из Go по сети compose и сгруппировал по SKU ({status})",
    )

    bad = {**order, "items": [{"sku": "bad", "quantity": 0, "price": 1}]}
    go_status, go_body = call("POST", f"{args.go}/api/v1/orders/validate", bad)
    py_status, py_body = call("POST", f"{args.py}/api/v1/orders/validate", bad)
    rules = [{(d["field"], d["rule"]) for d in b["error"]["details"]} for b in (go_body, py_body)]
    check(go_status == py_status == 422 and rules[0] == rules[1], "одинаковые ошибки валидации в Go и Python")

    print("\nвсе проверки пройдены")


if __name__ == "__main__":
    main()

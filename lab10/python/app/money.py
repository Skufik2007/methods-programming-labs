"""Денежные суммы в копейках — единственное место расчёта для моделей запроса и контракта Go.

Как и в Go (orders.TotalCents), суммы считаются в целых копейках: во float
0.1 + 0.2 != 0.3, а в копейках 10 + 20 == 30.
"""

from __future__ import annotations


def to_cents(price: float) -> int:
    return round(price * 100)


def has_at_most_two_decimals(price: float) -> bool:
    cents = price * 100
    return abs(cents - round(cents)) <= 1e-6


def line_total_cents(price: float, quantity: int) -> int:
    return to_cents(price) * quantity

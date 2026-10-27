"""Контракт данных Go-сервиса (средней сложности №5: сложные структуры JSON между сервисами).

Ответы Go (`POST /api/v1/orders`, `GET /api/v1/orders`) Python разбирает не как dict,
а по этим моделям. Вложенные структуры (покупатель → позиции → доставка) превращаются
в типизированные объекты, строки дат — в `date`/`datetime`, id — в `UUID`. Если Go вернул
не то (нет поля, не тот тип, сумма не сходится с позициями), это обнаруживается сразу
и превращается в 502 `upstream_contract_violation`, а не в KeyError где-то в обработчике.

Модели следуют принципу «терпимого читателя»: незнакомые поля игнорируются
(`extra="ignore"`), поэтому добавление нового поля в Go не ломает Python. Но
обязательные поля и типы проверяются строго.
"""

from __future__ import annotations

import datetime as dt
import uuid

from pydantic import BaseModel, ConfigDict, StrictInt, model_validator

from .money import line_total_cents


class _Contract(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class GoCustomer(_Contract):
    name: str
    email: str
    # В Go поле phone без omitempty: отсутствующий телефон приходит пустой строкой.
    phone: str = ""


class GoItem(_Contract):
    sku: str
    quantity: StrictInt
    price: float

    @property
    def total_cents(self) -> int:
        return line_total_cents(self.price, self.quantity)


class GoDelivery(_Contract):
    address: str
    date: dt.date


class GoOrder(_Contract):
    id: uuid.UUID
    owner: str
    customer: GoCustomer
    items: list[GoItem]
    delivery: GoDelivery
    comment: str = ""
    total_cents: StrictInt
    total: float
    created_at: dt.datetime

    @model_validator(mode="after")
    def total_matches_items(self) -> GoOrder:
        # Сумма — производное поле: проверяем, что вложенные позиции и итог согласованы.
        expected = sum(item.total_cents for item in self.items)
        if self.total_cents != expected:
            raise ValueError(f"total_cents={self.total_cents}, а по позициям выходит {expected}")
        return self


class GoOrdersPage(_Contract):
    orders: list[GoOrder]
    count: StrictInt

    @model_validator(mode="after")
    def count_matches(self) -> GoOrdersPage:
        if self.count != len(self.orders):
            raise ValueError(f"count={self.count}, а заказов в списке {len(self.orders)}")
        return self

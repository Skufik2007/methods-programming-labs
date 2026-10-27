"""Pydantic-модели заказа — те же правила, что и в Go (go/internal/orders).

Совпадение правил важно: на эндпоинте /api/v1/orders/validate сравнивается
производительность валидации FastAPI/Pydantic и Gin/validator.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StrictFloat, StrictInt, field_validator
from pydantic_core import PydanticCustomError

from .contract import GoOrder
from .money import has_at_most_two_decimals, line_total_cents

MAX_DELIVERY_DAYS = 90

# Регулярные выражения синхронизированы с Go (go/internal/orders/validation.go):
# SKU, телефон в формате E.164 и email — один и тот же шаблон в обоих сервисах.
SKU_PATTERN = r"^[A-Z]{3}-\d{3,6}$"
E164_PATTERN = r"^\+[1-9]\d{1,14}$"
# Email по стандарту HTML (WHATWG) с обязательной точкой в домене — как EmailPattern в Go.
EMAIL_PATTERN = (
    r"^[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?"
    r"(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)+$"
)


def today() -> dt.date:
    """«Сегодня» по UTC, как в Go. Вынесено в функцию, чтобы тесты могли его подменить."""
    return dt.datetime.now(dt.UTC).date()


def not_blank(value: str) -> str:
    """Аналог правила notblank в Go: строка из одних пробелов не считается заполненной."""
    if not value.strip():
        raise PydanticCustomError("notblank", "не может состоять только из пробелов")
    return value


class _Strict(BaseModel):
    # Как DisallowUnknownFields в Go: лишние поля — ошибка, а не тихий пропуск.
    model_config = ConfigDict(extra="forbid")


class Customer(_Strict):
    name: Annotated[str, Field(min_length=2, max_length=100), AfterValidator(not_blank)]
    email: Annotated[str, Field(max_length=254, pattern=EMAIL_PATTERN)]
    phone: Annotated[str, Field(pattern=E164_PATTERN)] | None = None

    @field_validator("phone", mode="before")
    @classmethod
    def empty_phone_is_absent(cls, v: object) -> object:
        # В Go у телефона omitempty: пустая строка означает «не указан», а не ошибку формата.
        return None if v == "" else v


class Item(_Strict):
    sku: Annotated[str, Field(pattern=SKU_PATTERN)]
    # Strict: строка "2" не превращается в число молча — так же ведёт себя Go.
    quantity: Annotated[StrictInt, Field(ge=1, le=100)]
    price: Annotated[StrictFloat | StrictInt, Field(gt=0, le=1_000_000)]

    @field_validator("price")
    @classmethod
    def two_decimals(cls, v: float) -> float:
        if not has_at_most_two_decimals(v):
            # Тип ошибки "money" совпадает с именем правила в Go.
            raise PydanticCustomError("money", "не больше двух знаков после запятой")
        return v


class Delivery(_Strict):
    address: Annotated[str, Field(min_length=5, max_length=300), AfterValidator(not_blank)]
    date: dt.date

    @field_validator("date")
    @classmethod
    def within_window(cls, v: dt.date) -> dt.date:
        start = today()
        if not start <= v <= start + dt.timedelta(days=MAX_DELIVERY_DAYS):
            raise PydanticCustomError(
                "delivery_date", "дата YYYY-MM-DD от сегодняшнего дня до +{days} дней", {"days": MAX_DELIVERY_DAYS}
            )
        return v


class OrderRequest(_Strict):
    customer: Customer
    items: Annotated[list[Item], Field(min_length=1, max_length=50)]
    delivery: Delivery
    comment: Annotated[str, Field(max_length=500)] = ""

    @field_validator("items")
    @classmethod
    def unique_skus(cls, items: list[Item]) -> list[Item]:
        seen: dict[str, int] = {}
        for i, item in enumerate(items):
            if item.sku in seen:
                # field в контексте — точный путь к полю (как в Go), иначе ошибка
                # указывала бы на весь список items.
                raise PydanticCustomError(
                    "unique_sku",
                    "SKU повторяется (совпадает с позицией items[{first}])",
                    {"field": f"items[{i}].sku", "first": seen[item.sku]},
                )
            seen[item.sku] = i
        return items

    def total_cents(self) -> int:
        return sum(line_total_cents(item.price, item.quantity) for item in self.items)


class ValidationResult(BaseModel):
    valid: bool
    items: int
    total_cents: int
    total: float


class UserInfo(BaseModel):
    username: str
    role: str
    expires_at: dt.datetime
    verified_by: str = Field(description="Кто проверил подпись токена")


class SkuTotal(BaseModel):
    sku: str
    quantity: int
    total_cents: int


class OrdersSummary(BaseModel):
    username: str
    count: int
    total_cents: int
    total: float
    items: int = Field(description="Число позиций во всех заказах")
    by_sku: list[SkuTotal] = Field(description="Агрегация по вложенным позициям, по убыванию суммы")
    last_order: GoOrder | None = Field(description="Последний заказ целиком, как его вернул Go")

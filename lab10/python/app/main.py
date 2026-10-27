"""FastAPI-сервис: проверяет JWT от Go-сервиса и обращается к нему с этим же токеном.

Swagger UI — /docs, схема OpenAPI — /openapi.json.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any, TypeVar

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from . import errors
from .auth import JWKSVerifier, require_claims
from .config import Settings
from .contract import GoOrder, GoOrdersPage
from .middleware import BodyLimitMiddleware, RequestContextMiddleware
from .models import OrderRequest, OrdersSummary, SkuTotal, UserInfo, ValidationResult

log = logging.getLogger("lab10")

Claims = Annotated[dict[str, Any], Depends(require_claims)]
ContractT = TypeVar("ContractT", bound=BaseModel)


def create_app(settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    """Фабрика приложения. transport позволяет подменить HTTP к Go-сервису в тестах."""
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Один клиент на всё приложение: пул соединений к Go-сервису переиспользуется.
        client = httpx.AsyncClient(base_url=settings.go_api_url, timeout=settings.upstream_timeout, transport=transport)
        app.state.client = client
        app.state.verifier = JWKSVerifier(
            client, settings.jwks_url, settings.jwt_issuer, settings.jwt_audience, settings.jwks_cache_ttl
        )
        app.state.ready = True
        log.info("сервис запущен, Go API: %s", settings.go_api_url)
        try:
            yield
        finally:
            # Сюда uvicorn приходит после того, как все активные запросы завершены.
            await client.aclose()
            log.info("ресурсы освобождены, сервис остановлен")

    app = FastAPI(
        title="Lab10 Python service",
        description="FastAPI-сервис: проверка JWT Go-сервиса по JWKS, валидация заказов на Pydantic.",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.ready = False
    errors.install(app)
    app.add_middleware(BodyLimitMiddleware)
    app.add_middleware(RequestContextMiddleware)

    @app.get("/ping", tags=["service"])
    async def ping() -> dict[str, str]:
        return {"message": "pong"}

    @app.get("/health/live", tags=["service"])
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready", tags=["service"], responses={503: {"description": "Сервис останавливается"}})
    async def ready(request: Request) -> JSONResponse:
        if not request.app.state.ready:
            return JSONResponse({"status": "shutting_down"}, status_code=503)
        return JSONResponse({"status": "ready"})

    @app.post("/api/v1/orders/validate", tags=["orders"], response_model=ValidationResult)
    async def validate_order(order: OrderRequest) -> ValidationResult:
        """Только проверяет заказ (те же правила, что в Go). Используется в сравнении производительности."""
        cents = order.total_cents()
        return ValidationResult(valid=True, items=len(order.items), total_cents=cents, total=cents / 100)

    @app.get("/api/v1/me", tags=["auth"], response_model=UserInfo)
    async def me(claims: Claims) -> UserInfo:
        """Данные из токена, подпись которого проверил Python по ключу Go-сервиса."""
        return UserInfo(
            username=claims["sub"],
            role=claims.get("role", ""),
            expires_at=claims["exp"],
            verified_by="python:jwks",
        )

    @app.post(
        "/api/v1/orders",
        tags=["orders"],
        status_code=status.HTTP_201_CREATED,
        response_model=GoOrder,
        responses={422: {"description": "Заказ не прошёл проверку в Python или в Go"}},
    )
    async def create_order(order: OrderRequest, request: Request, response: Response, claims: Claims) -> GoOrder:
        """Создаёт заказ в Go-сервисе: Python проверяет вложенную структуру, сериализует
        её в JSON для Go и разбирает ответ Go по типизированному контракту."""
        # mode="json": date -> "YYYY-MM-DD"; exclude_none: отсутствующий телефон не уходит как null.
        payload = order.model_dump(mode="json", exclude_none=True)
        resp = await _call_go(request, "POST", "/api/v1/orders", json=payload)
        if (passed := _client_error_from_go(resp)) is not None:
            return passed  # type: ignore[return-value]
        if resp.status_code != 201:
            raise _upstream_error(resp)
        created = _parse(GoOrder, resp)
        response.headers["Location"] = resp.headers.get("location", f"/api/v1/orders/{created.id}")
        return created

    @app.get("/api/v1/orders/summary", tags=["orders"], response_model=OrdersSummary)
    async def orders_summary(request: Request, claims: Claims) -> OrdersSummary:
        """Сводка по заказам пользователя: токен проверяется здесь и передаётся Go-сервису,
        ответ Go (список вложенных заказов) разбирается по контракту и агрегируется по SKU."""
        resp = await _call_go(request, "GET", "/api/v1/orders")
        if (passed := _client_error_from_go(resp)) is not None:
            return passed  # type: ignore[return-value]
        if resp.status_code != 200:
            raise _upstream_error(resp)
        page = _parse(GoOrdersPage, resp)

        by_sku: dict[str, SkuTotal] = {}
        for o in page.orders:
            for item in o.items:
                acc = by_sku.setdefault(item.sku, SkuTotal(sku=item.sku, quantity=0, total_cents=0))
                acc.quantity += item.quantity
                acc.total_cents += item.total_cents
        cents = sum(o.total_cents for o in page.orders)
        return OrdersSummary(
            username=claims["sub"],
            count=page.count,
            total_cents=cents,
            total=cents / 100,
            items=sum(len(o.items) for o in page.orders),
            by_sku=sorted(by_sku.values(), key=lambda s: (-s.total_cents, s.sku)),
            last_order=max(page.orders, key=lambda o: o.created_at, default=None),
        )

    return app


async def _call_go(request: Request, method: str, path: str, json: Any = None) -> httpx.Response:
    """Запрос к Go с токеном пользователя и X-Request-ID — по нему связываются логи двух сервисов."""
    client: httpx.AsyncClient = request.app.state.client
    headers = {
        "Authorization": request.headers["authorization"],
        "X-Request-ID": request.state.request_id,
    }
    try:
        return await client.request(method, path, headers=headers, json=json)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, {"code": "upstream_unavailable", "message": f"Go-сервис недоступен: {exc}"}
        ) from exc


def _client_error_from_go(resp: httpx.Response) -> JSONResponse | None:
    """Ошибку клиента от Go (401/403/422) отдаём как есть — формат ошибок у сервисов общий.

    Пробрасывается только тело в этом формате: если Go (или прокси перед ним) вернул
    HTML или другой JSON, это ошибка вышестоящего сервиса, и вызывающий код ответит 502.
    """
    if resp.status_code not in (401, 403, 422):
        return None
    try:
        body = resp.json()
    except ValueError:
        return None
    if not isinstance(body, dict) or not isinstance(body.get("error"), dict):
        return None
    headers = {}
    if "www-authenticate" in resp.headers:
        headers["WWW-Authenticate"] = resp.headers["www-authenticate"]
    return JSONResponse(body, status_code=resp.status_code, headers=headers)


def _upstream_error(resp: httpx.Response) -> HTTPException:
    return HTTPException(
        status.HTTP_502_BAD_GATEWAY, {"code": "upstream_error", "message": f"Go-сервис ответил {resp.status_code}"}
    )


def _parse(model: type[ContractT], resp: httpx.Response) -> ContractT:
    """Разбор тела ответа Go по модели контракта; несоответствие — 502, а не 500."""
    try:
        return model.model_validate_json(resp.content)
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:5])
        log.error("ответ Go не соответствует контракту %s: %s", model.__name__, problems)
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            {"code": "upstream_contract_violation", "message": f"ответ Go не соответствует контракту: {problems}"},
        ) from exc

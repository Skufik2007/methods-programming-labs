"""Заголовки безопасности дашборда.

Перенесено из ЛР №12 (`D:\\6 семестр\\Методы программирования\\lab12\\app\\main.py`,
класс ``SecurityHeadersMiddleware`` и ``CSP_STRICT``). Отличия от источника: нет
ослабленной политики для /docs (документация OpenAPI в дашборде отключена), в политику
добавлены ``form-action``/``base-uri`` (формы отправляются только на свой origin) и
заголовок ``X-Frame-Options`` для старых браузеров.
"""

from __future__ import annotations

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Только свои ресурсы, никаких inline-скриптов и стилей, встраивание в frame запрещено.
CSP_STRICT = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
)


class SecurityHeadersMiddleware:
    """ASGI-middleware: добавляет заголовки безопасности к каждому HTTP-ответу.

    Чистый ASGI (а не ``BaseHTTPMiddleware``): не буферизует тело ответа. Заголовки
    выставляются в момент ``http.response.start``, поэтому попадают и в ответы с ошибками
    (404/422), сформированные обработчиками.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_security_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Content-Security-Policy"] = CSP_STRICT
                headers["X-Content-Type-Options"] = "nosniff"
                # Не no-referrer: при нём Chromium шлёт POST формы с Origin: null, и проверка
                # CSRF в app.py отклоняет собственные формы дашборда.
                headers["Referrer-Policy"] = "same-origin"
                headers["X-Frame-Options"] = "DENY"
            await send(message)

        await self.app(scope, receive, send_with_security_headers)

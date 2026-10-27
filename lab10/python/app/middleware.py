"""ASGI-middleware: X-Request-ID, лог запросов и ограничение размера тела."""

from __future__ import annotations

import json
import logging
import re
import time
import uuid

from starlette.types import ASGIApp, Message, Receive, Scope, Send

log = logging.getLogger("lab10.access")

_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
MAX_BODY_BYTES = 1 << 20  # как в Go-сервисе


class RequestContextMiddleware:
    """Request ID и одна структурированная строка лога на запрос (аналог middleware в Go)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = dict(scope["headers"]).get(b"x-request-id", b"").decode("latin-1")
        request_id = incoming if _SAFE_REQUEST_ID.match(incoming) else str(uuid.uuid4())
        scope.setdefault("state", {})["request_id"] = request_id
        status_code = 500
        start = time.perf_counter()

        async def send_with_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                message.setdefault("headers", []).append((b"x-request-id", request_id.encode()))
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            record = {
                "method": scope["method"],
                "path": scope["path"],
                "status": status_code,
                "duration_ms": round((time.perf_counter() - start) * 1000, 3),
                "request_id": request_id,
            }
            user = scope["state"].get("user")
            if user:
                record["user"] = user
            log.log(logging.ERROR if status_code >= 500 else logging.INFO, json.dumps(record, ensure_ascii=False))


class BodyLimitMiddleware:
    """Отвечает 413, если тело больше лимита: по Content-Length сразу, иначе — по факту чтения."""

    def __init__(self, app: ASGIApp, max_bytes: int = MAX_BODY_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = dict(scope["headers"]).get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > self.max_bytes:
            await self._reject(send)
            return

        # Тело без Content-Length (chunked) считается по мере чтения. Исключение отсюда
        # бросать нельзя: FastAPI перехватывает любые ошибки чтения тела и превращает их
        # в 400 «There was an error parsing the body». Поэтому 413 отправляется здесь же,
        # приложению сообщается, что клиент отключился, а его собственный ответ отбрасывается.
        received = 0
        rejected = False

        async def limited_receive() -> Message:
            nonlocal received, rejected
            if rejected:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    rejected = True
                    await self._reject(send)
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message: Message) -> None:
            if not rejected:  # после 413 приложение уже не может ответить клиенту
                await send(message)

        await self.app(scope, limited_receive, guarded_send)

    async def _reject(self, send: Send) -> None:
        body = json.dumps(
            {"error": {"code": "body_too_large", "message": f"тело больше {self.max_bytes} байт"}},
            ensure_ascii=False,
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
            }
        )
        await send({"type": "http.response.body", "body": body})

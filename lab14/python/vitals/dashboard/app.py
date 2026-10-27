"""Приложение FastAPI: страница, WebSocket, REST и заголовки безопасности."""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from vitals.common import Settings, get_logger
from vitals.dashboard.bus import Bus, NatsBus
from vitals.dashboard.hub import Hub

log = get_logger("vitals.dashboard")
STATIC = Path(__file__).parent / "static"
_HOST_RE = re.compile(r"^[A-Za-z0-9.\-]+(:\d{1,5})?$|^\[[0-9A-Fa-f:]+\](:\d{1,5})?$")


def csp_for(host: str | None) -> str:
    """CSP; для connect-src добавляем ws:/wss: именно своего хоста (не любые ws:)."""
    connect = "'self'"
    if host and _HOST_RE.match(host):  # Host приходит от клиента - подставляем только проверенное значение
        connect += f" ws://{host} wss://{host}"
    return (
        f"default-src 'self'; script-src 'self'; style-src 'self'; connect-src {connect}; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    )


def create_app(settings: Settings | None = None, bus: Bus | None = None, client_queue: int = 32) -> FastAPI:
    settings = settings or Settings.from_env()
    hub = Hub(client_queue=client_queue)

    def on_message(msg: dict[str, Any]) -> None:
        kind = msg.get("type")
        if kind == "snapshot":
            hub.publish_snapshot(msg)
        elif kind == "alert":
            hub.publish_alert(msg)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.bus = bus or NatsBus(settings.nats_url)
        await app.state.bus.start(on_message)
        try:
            yield
        finally:
            await app.state.bus.close()

    app = FastAPI(title="vitals dashboard", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.hub = hub
    app.state.on_message = on_message

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        resp = await call_next(request)
        resp.headers["Content-Security-Policy"] = csp_for(request.headers.get("host"))
        resp.headers["Referrer-Policy"] = "same-origin"  # no-referrer давал Origin: null и ломал формы (ЛР13)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Cache-Control"] = "no-store"
        return resp

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html", media_type="text/html; charset=utf-8")

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        b = getattr(app.state, "bus", None)
        return {"status": "ok", "nats": bool(b and b.connected), "clients": hub.client_count}

    @app.get("/api/snapshot")
    async def snapshot() -> Any:
        return hub.snapshot or {"type": "snapshot", "ts": None, "patients": [], "counters": {}}

    @app.get("/api/alerts")
    async def alerts(limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)) -> JSONResponse:
        return JSONResponse(hub.alerts_page(limit, offset))

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        # Защита от кросс-сайтового WebSocket: Origin (если есть) должен совпадать с Host.
        origin = websocket.headers.get("origin")
        if origin and urlsplit(origin).netloc != websocket.headers.get("host"):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        client = hub.connect()
        # Сначала - текущее состояние, чтобы страница не ждала следующего снимка.
        await websocket.send_json(hub.hello())

        async def sender() -> None:
            while True:
                msg = await client.queue.get()
                if client.resync:  # были потери: шлём свежее состояние вместо пропущенных сообщений
                    client.resync = False
                    await websocket.send_json(hub.hello())
                    continue
                await websocket.send_json(msg)

        send_task = asyncio.create_task(sender())
        try:
            while True:  # входящие сообщения игнорируем, цикл нужен для обнаружения закрытия
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            send_task.cancel()
            hub.disconnect(client)
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await send_task

    return app

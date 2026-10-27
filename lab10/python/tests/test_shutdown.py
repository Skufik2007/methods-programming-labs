"""Graceful shutdown uvicorn (средней сложности №7, часть Python).

Сервер запускается по-настоящему (сокет, uvicorn), сигнал остановки
имитируется begin_shutdown — это та же логика, что в обработчике SIGTERM.
"""

from __future__ import annotations

import asyncio
import signal

import httpx
from conftest import FakeGo

from app.config import Settings
from app.main import create_app
from app.server import GracefulServer, build_server


async def _start(
    fake_go: FakeGo, settings: Settings, delay: float, drain: float = 0.0, shutdown_timeout: float = 5.0
) -> tuple[GracefulServer, asyncio.Task[None], str, asyncio.Event]:
    app = create_app(settings, transport=httpx.MockTransport(fake_go.handler))
    started = asyncio.Event()

    @app.get("/slow")
    async def slow() -> dict[str, str]:
        started.set()
        await asyncio.sleep(delay)
        return {"status": "done"}

    server = build_server(
        app, "127.0.0.1", 0, shutdown_timeout=shutdown_timeout, drain_delay=drain, log_level="warning"
    )
    task = asyncio.create_task(server.serve())
    while not server.started:  # noqa: ASYNC110 — у uvicorn нет события готовности, только флаг
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    return server, task, f"http://127.0.0.1:{port}", started


async def test_in_flight_request_completes(fake_go: FakeGo, settings: Settings) -> None:
    server, task, url, started = await _start(fake_go, settings, delay=0.5)
    app = server.config.app
    async with httpx.AsyncClient(base_url=url) as http:
        assert (await http.get("/health/ready")).status_code == 200
        pending = asyncio.create_task(http.get("/slow"))
        await started.wait()

        server.begin_shutdown()  # то же, что делает обработчик SIGTERM
        assert app.state.ready is False

        resp = await pending
        assert resp.status_code == 200
        assert resp.json() == {"status": "done"}

    await asyncio.wait_for(task, timeout=5)
    assert app.state.client.is_closed  # lifespan shutdown выполнен после запросов


async def test_drain_delay_keeps_serving(fake_go: FakeGo, settings: Settings) -> None:
    server, task, url, _ = await _start(fake_go, settings, delay=0, drain=0.5)
    async with httpx.AsyncClient(base_url=url) as http:
        server.begin_shutdown()
        await asyncio.sleep(0.15)
        # Во время drain: готовность уже 503, но запросы ещё обслуживаются.
        assert (await http.get("/health/ready")).status_code == 503
        assert (await http.get("/ping")).status_code == 200
    await asyncio.wait_for(task, timeout=5)


async def test_shutdown_timeout_cancels_long_request(fake_go: FakeGo, settings: Settings) -> None:
    server, task, url, started = await _start(fake_go, settings, delay=30, shutdown_timeout=1)
    async with httpx.AsyncClient(base_url=url) as http:
        pending = asyncio.create_task(http.get("/slow"))
        await started.wait()
        loop = asyncio.get_running_loop()
        begin = loop.time()
        server.begin_shutdown()
        await asyncio.wait_for(task, timeout=10)
        assert loop.time() - begin < 5, "SHUTDOWN_TIMEOUT не сработал"
        pending.cancel()


def test_shutdown_timeout_is_passed_exactly(settings: Settings) -> None:
    # Раньше int(0.5) or None давало None — бесконечное ожидание.
    for value in (0.5, 2.5, 0):
        server = build_server(create_app(settings), "127.0.0.1", 0, shutdown_timeout=value, drain_delay=0)
        assert server.config.timeout_graceful_shutdown == value


async def test_zero_shutdown_timeout_does_not_wait(fake_go: FakeGo, settings: Settings) -> None:
    server, task, url, started = await _start(fake_go, settings, delay=30, shutdown_timeout=0)
    async with httpx.AsyncClient(base_url=url) as http:
        pending = asyncio.create_task(http.get("/slow"))
        await started.wait()
        loop = asyncio.get_running_loop()
        begin = loop.time()
        server.begin_shutdown()
        await asyncio.wait_for(task, timeout=10)
        assert loop.time() - begin < 2, "при SHUTDOWN_TIMEOUT=0 сервер не должен ждать запросы"
        pending.cancel()


def test_signal_handler_marks_not_ready(settings: Settings) -> None:
    app = create_app(settings)
    app.state.ready = True
    server = build_server(app, "127.0.0.1", 0, shutdown_timeout=1, drain_delay=0)
    server.handle_exit(signal.SIGTERM, None)
    assert server.should_exit
    assert app.state.ready is False

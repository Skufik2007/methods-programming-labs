"""Запуск uvicorn с корректной остановкой (средней сложности №7, часть Python).

Что происходит по SIGINT/SIGTERM:
  1. handle_exit: /health/ready сразу начинает отвечать 503;
  2. пауза DRAIN_DELAY — сервер ещё принимает запросы, пока балансировщик
     замечает 503 и перестаёт слать трафик;
  3. uvicorn закрывает слушающий сокет, закрывает простаивающие keep-alive
     соединения и ждёт активные запросы не дольше SHUTDOWN_TIMEOUT;
  4. lifespan shutdown: закрывается HTTP-клиент к Go-сервису.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from types import FrameType

import uvicorn
from fastapi import FastAPI

log = logging.getLogger("lab10")


class GracefulServer(uvicorn.Server):
    def __init__(self, config: uvicorn.Config, app: FastAPI, drain_delay: float = 0.0) -> None:
        super().__init__(config)
        self._app = app
        self._drain_delay = drain_delay

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        """Обработчик SIGINT/SIGTERM, его регистрирует uvicorn."""
        if not self.should_exit:
            log.info("получен сигнал %s, начинаем graceful shutdown", sig)
        self._app.state.ready = False
        super().handle_exit(sig, frame)

    def begin_shutdown(self) -> None:
        """Та же остановка, но без сигнала (uvicorn после serve() повторно
        поднимает пойманный сигнал, что в тестах завершило бы процесс pytest)."""
        self._app.state.ready = False
        self.should_exit = True

    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        if self._drain_delay > 0:
            log.info("drain %.1f с: /health/ready уже 503, новые запросы ещё обслуживаются", self._drain_delay)
            await asyncio.sleep(self._drain_delay)
        await super().shutdown(sockets)


def build_server(
    app: FastAPI, host: str, port: int, shutdown_timeout: float, drain_delay: float, log_level: str = "info"
) -> GracefulServer:
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level=log_level,
        access_log=False,  # запросы логирует RequestContextMiddleware
        # Значение передаётся как есть: uvicorn ждёт через asyncio.wait_for, которому подходят
        # дробные секунды. int() отбрасывал бы дробную часть, а 0 превращался бы в None —
        # «ждать бесконечно». Теперь 0 означает «не ждать», как SHUTDOWN_TIMEOUT=0s в Go.
        timeout_graceful_shutdown=shutdown_timeout,  # type: ignore[arg-type]  # аннотация uvicorn: int
        # proxy_headers нужны за балансировщиком/в compose, чтобы видеть адрес клиента
        proxy_headers=True,
    )
    return GracefulServer(config, app, drain_delay)

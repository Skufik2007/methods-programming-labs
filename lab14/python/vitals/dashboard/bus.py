"""Источник сообщений для дашборда. `Bus` - интерфейс (в тестах подставляется заглушка), NatsBus - реальный."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Protocol

import nats

from vitals.common import get_logger

log = get_logger("vitals.dashboard")

Handler = Callable[[dict[str, Any]], None]


class Bus(Protocol):
    async def start(self, on_message: Handler) -> None: ...
    async def close(self) -> None: ...
    @property
    def connected(self) -> bool: ...


class NatsBus:
    """Подписка на core-субъекты vitals.stats и vitals.alerts."""

    def __init__(self, url: str) -> None:
        self.url = url
        self._nc: Any = None

    @property
    def connected(self) -> bool:
        return self._nc is not None and self._nc.is_connected

    async def start(self, on_message: Handler) -> None:
        async def cb(msg: Any) -> None:
            try:
                data = json.loads(msg.data)
            except ValueError:
                log.warning("битое сообщение", extra={"subject": msg.subject})
                return
            if isinstance(data, dict):
                on_message(data)

        # Не падаем, если NATS ещё недоступен: клиент переподключается сам, дашборд пока отдаёт пустой снимок.
        self._nc = await nats.connect(
            self.url, name="vitals-dashboard", max_reconnect_attempts=-1, allow_reconnect=True,
            connect_timeout=5,
        )
        await self._nc.subscribe("vitals.stats", cb=cb)
        await self._nc.subscribe("vitals.alerts", cb=cb)

    async def close(self) -> None:
        if self._nc is not None:
            await self._nc.close()

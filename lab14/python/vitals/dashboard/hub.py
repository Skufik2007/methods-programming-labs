"""Рассылка снимков и тревог WebSocket-клиентам. Без FastAPI и NATS - тестируется отдельно.

У каждого клиента своя ограниченная очередь: публикация не ждёт клиента (put_nowait), поэтому
медленный браузер не задерживает остальных. При переполнении выбрасывается самое старое сообщение,
а клиенту помечается «нужна ресинхронизация» - вместо потерянного он получит свежий снимок и
последние тревоги.
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Any

Message = dict[str, Any]


class Client:
    def __init__(self, maxsize: int) -> None:
        self.queue: asyncio.Queue[Message] = asyncio.Queue(maxsize=maxsize)
        self.resync = False
        self.dropped = 0

    def offer(self, msg: Message) -> None:
        if self.queue.full():
            self.queue.get_nowait()
            self.dropped += 1
            self.resync = True
        self.queue.put_nowait(msg)


class Hub:
    def __init__(self, client_queue: int = 32, alert_history: int = 1000) -> None:
        self.client_queue = client_queue
        self.snapshot: Message | None = None
        self.alerts: deque[Message] = deque(maxlen=alert_history)  # старые -> новые
        self.alerts_total = 0
        self._clients: set[Client] = set()

    @property
    def client_count(self) -> int:
        return len(self._clients)

    def connect(self) -> Client:
        c = Client(self.client_queue)
        self._clients.add(c)
        return c

    def disconnect(self, c: Client) -> None:
        self._clients.discard(c)

    def publish_snapshot(self, snap: Message) -> None:
        self.snapshot = snap
        for c in list(self._clients):
            c.offer(snap)

    def publish_alert(self, alert: Message) -> None:
        self.alerts.append(alert)
        self.alerts_total += 1
        for c in list(self._clients):
            c.offer(alert)

    def recent_alerts(self, n: int = 50) -> list[Message]:
        return list(self.alerts)[-n:]

    def hello(self) -> Message:
        """Начальное (и ресинхронизирующее) сообщение клиенту."""
        return {"type": "init", "snapshot": self.snapshot, "alerts": self.recent_alerts()}

    def alerts_page(self, limit: int, offset: int) -> Message:
        items = list(reversed(self.alerts))  # новейшие первыми
        return {"total": len(items), "limit": limit, "offset": offset, "items": items[offset : offset + limit]}

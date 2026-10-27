"""Фейковая шина для тестов оркестратора без NATS: агенты — функции-ответчики."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

from mas.common.bus import BusMessage
from mas.common.models import Task

# Ответчик шага: задача → (success, output), либо (success, output, поля результата),
# где поля перекрывают сгенерированные (например, {"retryable": True, "error": "..."}),
# либо None — агент молчит, оркестратор получит таймаут.
StepResponder = Callable[[Task], tuple[Any, ...] | None]


class _Sub:
    def __init__(self, bus: FakeBus, subject: str) -> None:
        self._bus, self._subject = bus, subject

    async def unsubscribe(self) -> None:
        self._bus.handlers.pop(self._subject, None)


class FakeBus:
    def __init__(
        self,
        steps: dict[str, StepResponder] | None = None,
        bids: list[dict[str, Any]] | Callable[[dict[str, Any]], list[dict[str, Any]]] | None = None,
        book: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
    ) -> None:
        self.steps = steps or {}
        self.bids = bids or []
        self.book = book or (lambda req: {"success": True, "output": {"appointment_id": "a-1"}})
        self.handlers: dict[str, Any] = {}
        self.published: list[Task] = []
        self.events: list[dict[str, Any]] = []
        self.book_requests: list[dict[str, Any]] = []
        self.auctions: list[dict[str, Any]] = []
        self.discarded: list[int] = []

    async def subscribe(self, subject: str, handler: Any) -> _Sub:
        self.handlers[subject] = handler
        return _Sub(self, subject)

    async def publish(self, subject: str, data: bytes, headers: dict[str, str] | None = None) -> None:
        if subject.startswith("events."):
            self.events.append(json.loads(data))

    async def publish_task(self, task: Task, *, inject_trace: bool = True) -> int:
        self.published.append(task)
        responder = self.steps.get(task.step)
        answer = responder(task) if responder else None
        if answer is not None:
            success, output, *rest = answer
            extra = rest[0] if rest else {}
            result = {
                "task_id": task.task_id, "run_id": task.run_id, "step": task.step, "agent_id": f"{task.step}-1",
                "success": success, "output": output, "error": "" if success else "ошибка", **extra,
            }
            msg = BusMessage(f"results.{task.run_id}", json.dumps(result).encode())
            asyncio.get_running_loop().call_soon(lambda: asyncio.ensure_future(self.handlers["results.>"](msg)))
        return len(self.published)

    async def discard_task(self, seq: int) -> None:
        self.discarded.append(seq)

    async def collect(self, subject: str, data: bytes, window: float) -> list[BusMessage]:
        req = json.loads(data)
        self.auctions.append(req)
        bids = self.bids(req) if callable(self.bids) else self.bids
        return [BusMessage("inbox", json.dumps({**bid, "auction_id": req["auction_id"]}).encode()) for bid in bids]

    async def request(self, subject: str, data: bytes, timeout: float) -> BusMessage:
        req = json.loads(data)
        self.book_requests.append(req)
        reply = self.book(req)
        if reply is None:  # агент записи молчит → оркестратор получает TimeoutError, как от NatsBus
            raise TimeoutError(f"нет ответа на {subject}")
        return BusMessage("inbox", json.dumps(reply).encode())

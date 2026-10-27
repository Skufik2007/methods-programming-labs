"""Шина сообщений: протокол `MessageBus` и реализация поверх nats-py (core + JetStream).

Оркестратор и агенты зависят только от протокола, поэтому в тестах шину можно подменить
фейком. Подключение выполняется явным `connect()`; при импорте ничего не создаётся.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

import nats
import nats.errors
from nats.aio.client import Client as NatsClient
from nats.js.api import AckPolicy, ConsumerConfig, RetentionPolicy, StorageType, StreamConfig
from nats.js.errors import APIError, NotFoundError
from nats.js.errors import Error as JsError

from mas.common.models import Event, Heartbeat, Result, Task
from mas.common.settings import LLM_STEPS
from mas.common.telemetry import inject_traceparent

log = logging.getLogger("mas.bus")

STREAM_NAME = "TASKS"
TASK_STEPS = ("intake", "triage", "reminder", "feedback", "feedback_reply")
SUBJECT_HEARTBEAT = "agents.heartbeat"
_JS_NO_MESSAGE_FOUND = 10057  # JSStreamMsgDeleteFailedF: сообщения с таким номером уже нет


@dataclass
class BusMessage:
    """Сообщение core NATS в виде, не зависящем от nats-py."""

    subject: str
    data: bytes
    headers: dict[str, str] = field(default_factory=dict)
    reply: str = ""


@runtime_checkable
class TaskMessage(Protocol):
    """Сообщение из JetStream: данные и управление подтверждением."""

    data: bytes
    headers: dict[str, str]

    async def ack(self) -> None: ...
    async def nak(self) -> None: ...
    async def term(self) -> None: ...
    async def in_progress(self) -> None: ...


Handler = Callable[[BusMessage], Awaitable[None]]


class Subscription(Protocol):
    async def unsubscribe(self) -> None: ...


class MessageBus(Protocol):
    async def connect(self) -> None: ...
    async def close(self) -> None: ...
    async def ensure_stream(self, steps: tuple[str, ...] = TASK_STEPS) -> None: ...
    async def publish_task(self, task: Task, *, inject_trace: bool = True) -> int:
        """Публикует задачу в поток TASKS, возвращает номер сообщения в потоке."""
        ...

    async def discard_task(self, seq: int) -> None:
        """Удаляет из потока задачу, ответа на которую оркестратор больше не ждёт."""
        ...
    async def publish(self, subject: str, data: bytes, headers: dict[str, str] | None = None) -> None: ...
    async def subscribe(self, subject: str, handler: Handler) -> Subscription: ...
    async def request(self, subject: str, data: bytes, timeout: float) -> BusMessage:
        """Один ответ; по истечении времени — `TimeoutError`."""
        ...

    async def collect(self, subject: str, data: bytes, window: float) -> list[BusMessage]:
        """Публикует запрос с `reply` и собирает ВСЕ ответы за `window` секунд (аукцион)."""
        ...

    async def fetch_tasks(self, step: str, batch: int, timeout: float) -> list[TaskMessage]: ...


async def publish_event(bus: MessageBus, event: Event) -> None:
    await bus.publish(f"events.{event.source}", event.model_dump_json().encode())


async def publish_heartbeat(bus: MessageBus, hb: Heartbeat) -> None:
    await bus.publish(SUBJECT_HEARTBEAT, hb.model_dump_json().encode())


async def publish_result(bus: MessageBus, result: Result) -> None:
    await bus.publish(f"results.{result.run_id}", result.model_dump_json().encode())


def _trace_headers() -> dict[str, str]:
    """Заголовки с traceparent текущего спана: аукцион и бронь — часть трассы запуска."""
    headers: dict[str, str] = {}
    inject_traceparent(headers)
    return headers


def ack_wait_for(step: str) -> float:
    """AckWait из раздела 3: 120 с для LLM-шагов, 30 с для остальных."""
    return 120.0 if step in LLM_STEPS else 30.0


class NatsBus:
    """Реализация `MessageBus` на nats-py."""

    def __init__(self, url: str, name: str = "mas") -> None:
        self._url = url
        self._name = name
        self._nc: NatsClient | None = None
        self._js = None
        self._pulls: dict[str, object] = {}

    @property
    def nc(self) -> NatsClient:
        if self._nc is None:
            raise RuntimeError("шина не подключена: вызовите connect()")
        return self._nc

    async def connect(self) -> None:
        self._nc = await nats.connect(
            self._url,
            name=self._name,
            max_reconnect_attempts=-1,
            reconnect_time_wait=1,
        )
        self._js = self._nc.jetstream()

    async def close(self) -> None:
        if self._nc is not None:
            try:
                await self._nc.drain()
            except nats.errors.Error:
                await self._nc.close()
            self._nc = None
            self._js = None
            self._pulls.clear()

    async def ensure_stream(self, steps: tuple[str, ...] = TASK_STEPS) -> None:
        """Идемпотентно создаёт поток TASKS и консьюмеры step-<step> (раздел 3)."""
        js = self._js
        if js is None:
            raise RuntimeError("шина не подключена: вызовите connect()")
        config = StreamConfig(
            name=STREAM_NAME,
            subjects=["tasks.>"],
            retention=RetentionPolicy.WORK_QUEUE,
            storage=StorageType.FILE,
            max_age=24 * 3600,
        )
        try:
            await js.stream_info(STREAM_NAME)
            await js.update_stream(config)
        except NotFoundError:
            await js.add_stream(config)
        for step in steps:
            durable = f"step-{step}"
            try:
                await js.consumer_info(STREAM_NAME, durable)
            except NotFoundError:
                cfg = ConsumerConfig(
                    durable_name=durable,
                    filter_subject=f"tasks.{step}",
                    ack_policy=AckPolicy.EXPLICIT,
                    ack_wait=ack_wait_for(step),
                    max_deliver=5,
                )
                try:
                    await js.add_consumer(STREAM_NAME, cfg)
                except JsError as exc:
                    # Параллельный старт другого процесса мог создать тот же консьюмер.
                    log.warning("consumer %s: %s", durable, exc)

    async def consumer_stats(self, steps: tuple[str, ...] = TASK_STEPS) -> list[dict[str, int | str]]:
        """Состояние консьюмеров `step-*` для дашборда: ожидают, в обработке, повторные доставки.

        Отсутствующий консьюмер пропускается. Метод не входит в протокол `MessageBus`
        (фейковые шины в тестах его не обязаны иметь) — вызывающий проверяет через getattr.
        """
        if self._js is None:
            raise RuntimeError("шина не подключена: вызовите connect()")
        rows: list[dict[str, int | str]] = []
        for step in steps:
            try:
                info = await self._js.consumer_info(STREAM_NAME, f"step-{step}")
            except NotFoundError:
                continue
            rows.append(
                {
                    "step": step,
                    "consumer": f"step-{step}",
                    "pending": info.num_pending,
                    "ack_pending": info.num_ack_pending,
                    "redelivered": info.num_redelivered,
                }
            )
        return rows

    async def publish_task(self, task: Task, *, inject_trace: bool = True) -> int:
        if self._js is None:
            raise RuntimeError("шина не подключена: вызовите connect()")
        headers = {"Nats-Msg-Id": task.msg_id}
        if inject_trace:
            inject_traceparent(headers)
        ack = await self._js.publish(f"tasks.{task.step}", task.model_dump_json().encode(), headers=headers)
        return ack.seq

    async def discard_task(self, seq: int) -> None:
        if self._js is None:
            raise RuntimeError("шина не подключена: вызовите connect()")
        try:
            await self._js.delete_msg(STREAM_NAME, seq)
        except NotFoundError:
            pass  # задачу уже забрал и подтвердил агент — удалять нечего
        except APIError as exc:
            # nats-py отдаёт «no message found» при удалении как ServerError (500), а не NotFoundError.
            if exc.err_code != _JS_NO_MESSAGE_FOUND:
                raise

    async def publish(self, subject: str, data: bytes, headers: dict[str, str] | None = None) -> None:
        await self.nc.publish(subject, data, headers=headers)

    async def subscribe(self, subject: str, handler: Handler) -> Subscription:
        async def _cb(msg) -> None:
            await handler(BusMessage(msg.subject, msg.data, dict(msg.headers or {}), msg.reply or ""))

        return await self.nc.subscribe(subject, cb=_cb)

    async def request(self, subject: str, data: bytes, timeout: float) -> BusMessage:
        try:
            msg = await self.nc.request(subject, data, timeout=timeout, headers=_trace_headers())
        except nats.errors.TimeoutError as exc:
            raise TimeoutError(f"нет ответа на {subject}") from exc
        except nats.errors.NoRespondersError as exc:
            raise TimeoutError(f"нет подписчиков на {subject}") from exc
        return BusMessage(msg.subject, msg.data, dict(msg.headers or {}), msg.reply or "")

    async def collect(self, subject: str, data: bytes, window: float) -> list[BusMessage]:
        # request() в nats-py возвращает только первый ответ, поэтому собираем сами.
        nc = self.nc
        inbox = nc.new_inbox()
        sub = await nc.subscribe(inbox)
        replies: list[BusMessage] = []
        try:
            await nc.flush()
            await nc.publish(subject, data, reply=inbox, headers=_trace_headers())
            deadline = time.monotonic() + window
            while (left := deadline - time.monotonic()) > 0:
                try:
                    msg = await sub.next_msg(timeout=left)
                except (TimeoutError, nats.errors.TimeoutError):
                    break
                replies.append(BusMessage(msg.subject, msg.data, dict(msg.headers or {}), msg.reply or ""))
        finally:
            await sub.unsubscribe()
        return replies

    async def fetch_tasks(self, step: str, batch: int, timeout: float) -> list[TaskMessage]:
        if self._js is None:
            raise RuntimeError("шина не подключена: вызовите connect()")
        sub = self._pulls.get(step)
        if sub is None:
            sub = await self._js.pull_subscribe(f"tasks.{step}", durable=f"step-{step}", stream=STREAM_NAME)
            self._pulls[step] = sub
        try:
            return await sub.fetch(batch, timeout=timeout)  # type: ignore[attr-defined]
        except (TimeoutError, nats.errors.TimeoutError):
            return []

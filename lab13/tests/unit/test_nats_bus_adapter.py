"""Адаптер NatsBus (mas/common/bus.py) без сервера: nats.connect подменён фейковым соединением и JetStream.

Проверяется логика адаптера: создание потока и консьюмеров (раздел 3), удаление задач, маппинг
таймаутов, сбор ставок аукциона, кеш pull-подписок. Работа самого NATS здесь не проверяется
(это задача интеграционных тестов)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import nats
import nats.errors
import pytest
from nats.js.errors import APIError, NotFoundError
from nats.js.errors import Error as JsError
from opentelemetry.sdk.trace import TracerProvider

from mas.common import bus as bus_module
from mas.common.bus import STREAM_NAME, BusMessage, NatsBus, ack_wait_for, publish_heartbeat, publish_result
from mas.common.models import Heartbeat, Result, Task


class FakeMsg:
    def __init__(self, subject: str, data: bytes, headers: dict[str, str] | None = None, reply: str = "") -> None:
        self.subject = subject
        self.data = data
        self.headers = headers
        self.reply = reply


class FakeSub:
    def __init__(self, messages: list[FakeMsg] | None = None, batches: list[list[FakeMsg]] | None = None) -> None:
        self.messages = list(messages or [])
        self.batches = list(batches or [])
        self.unsubscribed = False
        self.fetch_calls: list[tuple[int, float]] = []

    async def next_msg(self, timeout: float) -> FakeMsg:
        if self.messages:
            return self.messages.pop(0)
        raise nats.errors.TimeoutError()

    async def unsubscribe(self) -> None:
        self.unsubscribed = True

    async def fetch(self, batch: int, timeout: float) -> list[FakeMsg]:
        self.fetch_calls.append((batch, timeout))
        if not self.batches:
            raise nats.errors.TimeoutError()
        return self.batches.pop(0)


class FakeNC:
    def __init__(self, js: FakeJS) -> None:
        self.js = js
        self.published: list[tuple[str, bytes, str, dict[str, str] | None]] = []
        self.requests: list[tuple[str, bytes, float, dict[str, str] | None]] = []
        self.request_reply: FakeMsg | None = None
        self.request_error: Exception | None = None
        self.inbox_messages: list[FakeMsg] = []
        self.callbacks: dict[str, Any] = {}
        self.drain_error: Exception | None = None
        self.drained = False
        self.closed = False

    def jetstream(self) -> FakeJS:
        return self.js

    async def publish(self, subject: str, data: bytes, reply: str = "", headers: dict[str, str] | None = None) -> None:
        self.published.append((subject, data, reply, headers))

    async def subscribe(self, subject: str, cb: Any = None) -> FakeSub:
        if cb is not None:
            self.callbacks[subject] = cb
        if subject.startswith("_INBOX"):
            return FakeSub(messages=self.inbox_messages)
        return FakeSub()

    async def request(
        self, subject: str, data: bytes, timeout: float, headers: dict[str, str] | None = None
    ) -> FakeMsg:
        self.requests.append((subject, data, timeout, headers))
        if self.request_error is not None:
            raise self.request_error
        assert self.request_reply is not None
        return self.request_reply

    def new_inbox(self) -> str:
        return "_INBOX.test"

    async def flush(self) -> None:
        return None

    async def drain(self) -> None:
        if self.drain_error is not None:
            raise self.drain_error
        self.drained = True

    async def close(self) -> None:
        self.closed = True


class FakeJS:
    def __init__(self, *, stream_exists: bool = True, consumers: dict[str, Any] | None = None) -> None:
        self.stream_exists = stream_exists
        self.consumers = consumers or {}
        self.updated: list[Any] = []
        self.added_streams: list[Any] = []
        self.added_consumers: list[Any] = []
        self.add_consumer_error: Exception | None = None
        self.published: list[tuple[str, bytes, dict[str, str] | None]] = []
        self.delete_error: Exception | None = None
        self.deleted: list[tuple[str, int]] = []
        self.pull_subjects: list[tuple[str, str, str]] = []
        self.pull_sub = FakeSub(batches=[[FakeMsg("tasks.triage", b"{}")]])

    async def stream_info(self, name: str) -> Any:
        if not self.stream_exists:
            raise NotFoundError()
        return SimpleNamespace(name=name)

    async def update_stream(self, config: Any) -> None:
        self.updated.append(config)

    async def add_stream(self, config: Any) -> None:
        self.added_streams.append(config)
        self.stream_exists = True

    async def consumer_info(self, stream: str, durable: str) -> Any:
        if durable not in self.consumers:
            raise NotFoundError()
        return self.consumers[durable]

    async def add_consumer(self, stream: str, config: Any) -> None:
        if self.add_consumer_error is not None:
            raise self.add_consumer_error
        self.added_consumers.append(config)

    async def publish(self, subject: str, data: bytes, headers: dict[str, str] | None = None) -> Any:
        self.published.append((subject, data, headers))
        return SimpleNamespace(seq=len(self.published) * 10)

    async def delete_msg(self, stream: str, seq: int) -> None:
        if self.delete_error is not None:
            raise self.delete_error
        self.deleted.append((stream, seq))

    async def pull_subscribe(self, subject: str, durable: str, stream: str) -> FakeSub:
        self.pull_subjects.append((subject, durable, stream))
        return self.pull_sub


@pytest.fixture
def fake_nats(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"js": FakeJS()}
    state["nc"] = FakeNC(state["js"])

    async def connect(url: str, **kwargs: Any) -> FakeNC:
        state["url"] = url
        state["kwargs"] = kwargs
        return state["nc"]

    monkeypatch.setattr(nats, "connect", connect)
    return state


async def _connected(fake_nats: dict[str, Any]) -> NatsBus:
    bus = NatsBus("nats://fake:4222", name="test")
    await bus.connect()
    return bus


async def test_connect_passes_reconnect_options(fake_nats: dict[str, Any]) -> None:
    bus = await _connected(fake_nats)
    assert fake_nats["url"] == "nats://fake:4222"
    assert fake_nats["kwargs"]["name"] == "test"
    assert fake_nats["kwargs"]["max_reconnect_attempts"] == -1
    assert bus.nc is fake_nats["nc"]


def test_nc_property_requires_connect() -> None:
    with pytest.raises(RuntimeError, match="connect"):
        _ = NatsBus("nats://x").nc


async def test_methods_require_connection() -> None:
    bus = NatsBus("nats://x")
    task = Task(run_id="r", step="triage", kind="triage")
    with pytest.raises(RuntimeError):
        await bus.ensure_stream()
    with pytest.raises(RuntimeError):
        await bus.consumer_stats()
    with pytest.raises(RuntimeError):
        await bus.publish_task(task)
    with pytest.raises(RuntimeError):
        await bus.discard_task(1)
    with pytest.raises(RuntimeError):
        await bus.fetch_tasks("triage", 1, 0.1)


async def test_ensure_stream_creates_stream_and_missing_consumers(fake_nats: dict[str, Any]) -> None:
    js: FakeJS = fake_nats["js"]
    js.stream_exists = False
    bus = await _connected(fake_nats)
    await bus.ensure_stream()
    assert len(js.added_streams) == 1
    stream = js.added_streams[0]
    assert stream.name == STREAM_NAME
    assert stream.subjects == ["tasks.>"]
    assert js.updated == []
    names = {c.durable_name: c for c in js.added_consumers}
    assert set(names) == {"step-intake", "step-triage", "step-reminder", "step-feedback", "step-feedback_reply"}
    intake = names["step-intake"]
    assert intake.filter_subject == "tasks.intake"
    assert intake.max_deliver == 5
    assert intake.ack_wait == ack_wait_for("intake")
    assert names["step-triage"].ack_wait == ack_wait_for("triage") == 30.0


async def test_ensure_stream_updates_existing_and_keeps_existing_consumers(fake_nats: dict[str, Any]) -> None:
    js: FakeJS = fake_nats["js"]
    js.consumers = {"step-triage": SimpleNamespace(num_pending=0, num_ack_pending=0, num_redelivered=0)}
    bus = await _connected(fake_nats)
    await bus.ensure_stream(steps=("triage",))
    assert len(js.updated) == 1
    assert js.added_streams == []
    assert js.added_consumers == []


async def test_consumer_race_is_logged_not_raised(fake_nats: dict[str, Any]) -> None:
    js: FakeJS = fake_nats["js"]
    js.add_consumer_error = JsError("consumer already exists")
    bus = await _connected(fake_nats)
    await bus.ensure_stream(steps=("triage",))  # не должно упасть


async def test_consumer_stats_skips_missing_consumers(fake_nats: dict[str, Any]) -> None:
    js: FakeJS = fake_nats["js"]
    js.consumers = {"step-triage": SimpleNamespace(num_pending=5, num_ack_pending=2, num_redelivered=1)}
    bus = await _connected(fake_nats)
    rows = await bus.consumer_stats(("triage", "booking"))
    assert rows == [{"step": "triage", "consumer": "step-triage", "pending": 5, "ack_pending": 2,
                     "redelivered": 1}]


async def test_publish_task_sets_msg_id_and_traceparent(fake_nats: dict[str, Any]) -> None:
    js: FakeJS = fake_nats["js"]
    bus = await _connected(fake_nats)
    task = Task(run_id="run-1", step="triage", kind="triage", attempt=2)
    tracer = TracerProvider().get_tracer("test")
    with tracer.start_as_current_span("pipeline.run"):
        seq = await bus.publish_task(task)
    subject, data, headers = js.published[-1]
    assert subject == "tasks.triage"
    assert headers is not None
    assert headers["Nats-Msg-Id"] == task.msg_id
    assert headers["traceparent"].startswith("00-")
    assert Task.model_validate_json(data) == task
    assert seq == 10


async def test_publish_task_without_trace_injection(fake_nats: dict[str, Any]) -> None:
    js: FakeJS = fake_nats["js"]
    bus = await _connected(fake_nats)
    tracer = TracerProvider().get_tracer("test")
    with tracer.start_as_current_span("pipeline.run"):
        await bus.publish_task(Task(run_id="r", step="triage", kind="triage"), inject_trace=False)
    _, _, headers = js.published[-1]
    assert "traceparent" not in headers


async def test_discard_task_removes_and_ignores_missing(fake_nats: dict[str, Any]) -> None:
    js: FakeJS = fake_nats["js"]
    bus = await _connected(fake_nats)
    await bus.discard_task(7)
    assert js.deleted == [(STREAM_NAME, 7)]
    js.delete_error = NotFoundError()
    await bus.discard_task(8)  # уже подтверждена — не ошибка
    js.delete_error = APIError(err_code=10057)
    await bus.discard_task(9)  # «no message found» — не ошибка
    js.delete_error = APIError(err_code=500)
    with pytest.raises(APIError):
        await bus.discard_task(10)


async def test_publish_and_subscribe_convert_messages(fake_nats: dict[str, Any]) -> None:
    nc: FakeNC = fake_nats["nc"]
    bus = await _connected(fake_nats)
    await bus.publish("events.x", b"{}", headers={"a": "b"})
    assert nc.published == [("events.x", b"{}", "", {"a": "b"})]

    seen: list[BusMessage] = []

    async def handler(msg: BusMessage) -> None:
        seen.append(msg)

    await bus.subscribe("results.>", handler)
    await nc.callbacks["results.>"](FakeMsg("results.r1", b"data", {"h": "1"}, "_reply"))
    assert seen == [BusMessage("results.r1", b"data", {"h": "1"}, "_reply")]


async def test_request_returns_reply_and_maps_timeouts(fake_nats: dict[str, Any]) -> None:
    nc: FakeNC = fake_nats["nc"]
    bus = await _connected(fake_nats)
    nc.request_reply = FakeMsg("inbox", b'{"success": true}', None, "")
    reply = await bus.request("agent.booking-1.book", b"{}", 2.0)
    assert reply.data == b'{"success": true}'
    assert reply.headers == {}
    assert nc.requests[0][2] == 2.0

    nc.request_error = nats.errors.TimeoutError()
    with pytest.raises(TimeoutError, match="нет ответа"):
        await bus.request("agent.x.book", b"{}", 1.0)
    nc.request_error = nats.errors.NoRespondersError()
    with pytest.raises(TimeoutError, match="нет подписчиков"):
        await bus.request("agent.x.book", b"{}", 1.0)


async def test_collect_gathers_replies_until_window_ends(fake_nats: dict[str, Any]) -> None:
    nc: FakeNC = fake_nats["nc"]
    nc.inbox_messages = [FakeMsg("inbox", b'{"agent_id": "a"}'), FakeMsg("inbox", b'{"agent_id": "b"}')]
    bus = await _connected(fake_nats)
    replies = await bus.collect("auction.booking", b"{}", 0.05)
    assert [r.data for r in replies] == [b'{"agent_id": "a"}', b'{"agent_id": "b"}']
    subject, data, reply, headers = nc.published[-1]
    assert subject == "auction.booking"
    assert reply == "_INBOX.test"


async def test_fetch_tasks_caches_pull_subscription_and_maps_timeout(fake_nats: dict[str, Any]) -> None:
    js: FakeJS = fake_nats["js"]
    js.pull_sub.batches = [[FakeMsg("tasks.triage", b"{}")], []]
    bus = await _connected(fake_nats)
    first = await bus.fetch_tasks("triage", 2, 0.5)
    assert len(first) == 1
    assert await bus.fetch_tasks("triage", 2, 0.5) == []  # пустой батч — не ошибка
    assert await bus.fetch_tasks("triage", 2, 0.5) == []  # таймаут NATS → пусто
    assert js.pull_subjects == [("tasks.triage", "step-triage", STREAM_NAME)]
    assert js.pull_sub.fetch_calls[0] == (2, 0.5)


async def test_close_drains_then_falls_back_to_close(fake_nats: dict[str, Any]) -> None:
    nc: FakeNC = fake_nats["nc"]
    bus = await _connected(fake_nats)
    await bus.close()
    assert nc.drained is True
    assert bus._nc is None  # noqa: SLF001 - проверка сброса состояния после close

    nc2 = FakeNC(fake_nats["js"])
    nc2.drain_error = nats.errors.Error("drain failed")
    fake_nats["nc"] = nc2
    bus2 = await _connected(fake_nats)
    await bus2.close()
    assert nc2.closed is True


async def test_close_without_connection_is_noop() -> None:
    await NatsBus("nats://x").close()


async def test_helper_publishers_use_subjects(fake_nats: dict[str, Any]) -> None:
    nc: FakeNC = fake_nats["nc"]
    bus = await _connected(fake_nats)
    await publish_result(bus, Result(task_id="t", run_id="run-9", step="triage", success=True))
    await publish_heartbeat(bus, Heartbeat(agent_id="triage-1", role="triage"))
    subjects = [entry[0] for entry in nc.published]
    assert subjects == ["results.run-9", "agents.heartbeat"]


def test_ack_wait_constants_are_seconds() -> None:
    assert bus_module.STREAM_NAME == "TASKS"
    assert ack_wait_for("feedback_reply") == 120.0

"""Находка №5: после таймаута попытки задача оставалась в очереди JetStream. На 40 демо-обращений
без агента триажа в очереди накопилось 121 задача: агенты потом выполняли работу для давно
проваленных запусков, а автомасштабировщик поднимал экземпляры под «мёртвый» backlog.
Оркестратор должен убирать из потока свою задачу, на которую перестал ждать ответ."""

from typing import Any

from opentelemetry.sdk.trace import TracerProvider

from mas.common.models import Task
from mas.common.settings import Settings
from mas.orchestrator.orchestrator import Orchestrator


class _Sub:
    async def unsubscribe(self) -> None:
        return None


class SilentBus:
    """Шина, в которой агенты не отвечают: каждая попытка заканчивается таймаутом."""

    def __init__(self) -> None:
        self.published: list[Task] = []
        self.discarded: list[int] = []

    async def subscribe(self, subject: str, handler: Any) -> _Sub:
        return _Sub()

    async def publish(self, subject: str, data: bytes, headers: dict[str, str] | None = None) -> None:
        return None

    async def publish_task(self, task: Task, *, inject_trace: bool = True) -> int:
        self.published.append(task)
        return len(self.published)  # номер сообщения в потоке

    async def discard_task(self, seq: int) -> None:
        self.discarded.append(seq)


async def test_timed_out_attempts_are_removed_from_queue() -> None:
    bus = SilentBus()
    settings = Settings(step_timeout_s=0.05, llm_step_timeout_s=0.05, max_attempts=3, otel_sdk_disabled=True)
    orch = Orchestrator(bus, settings, tracer=TracerProvider().get_tracer("test"))  # type: ignore[arg-type]
    await orch.start()
    run_id = await orch.start_case(
        {"patient_id": "p-1", "patient_name": "Иванова", "age": 30, "phone": "+7", "complaint": "болит горло"}
    )
    run = await orch.wait_run(run_id, timeout=5)
    await orch.stop()
    assert run.status == "failed"
    assert len(bus.published) == 3
    assert bus.discarded == [1, 2, 3]

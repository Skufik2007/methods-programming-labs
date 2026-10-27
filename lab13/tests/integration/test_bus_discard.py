"""Находка №5 на настоящем JetStream: удалённая задача исчезает из потока TASKS."""

import pytest
from nats.js.errors import NotFoundError

from mas.common.bus import STREAM_NAME, NatsBus
from mas.common.models import Task, new_id


async def test_discard_task_removes_message(nats_url: str) -> None:
    bus = NatsBus(nats_url)
    await bus.connect()
    try:
        await bus.ensure_stream(steps=())
        # Шаг без консьюмера: сообщение гарантированно лежит в потоке, его никто не заберёт.
        seq = await bus.publish_task(Task(run_id=new_id(), step="itest", kind="itest"))
        js = bus.nc.jetstream()
        assert (await js.get_msg(STREAM_NAME, seq)).seq == seq
        await bus.discard_task(seq)
        with pytest.raises(NotFoundError):
            await js.get_msg(STREAM_NAME, seq)
        await bus.discard_task(seq)  # повторное удаление не ошибка
    finally:
        await bus.close()

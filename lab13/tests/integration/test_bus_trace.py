"""Находка №1: аукцион и бронь уходили без traceparent — спаны агентов записи
оказывались в отдельных трассах, а не в трассе запуска."""

import asyncio

from opentelemetry.sdk.trace import TracerProvider

from mas.common.bus import BusMessage, NatsBus
from mas.common.models import new_id


async def _capture(bus: NatsBus, subject: str, reply: bytes) -> list[BusMessage]:
    seen: list[BusMessage] = []

    async def handler(msg: BusMessage) -> None:
        seen.append(msg)
        await bus.publish(msg.reply, reply)

    await bus.subscribe(subject, handler)
    return seen


def _trace_id_of(traceparent: str) -> str:
    return traceparent.split("-")[1]


async def test_collect_and_request_carry_traceparent(nats_url: str) -> None:
    bus = NatsBus(nats_url)
    await bus.connect()
    try:
        auction_subject, book_subject = f"test.auction.{new_id()}", f"test.book.{new_id()}"
        auctions = await _capture(bus, auction_subject, b"{}")
        books = await _capture(bus, book_subject, b"{}")
        tracer = TracerProvider().get_tracer("test")
        with tracer.start_as_current_span("pipeline.run") as span:
            expected = format(span.get_span_context().trace_id, "032x")
            await bus.collect(auction_subject, b"{}", 0.2)
            await bus.request(book_subject, b"{}", 1.0)
        await asyncio.sleep(0.05)
        assert _trace_id_of(auctions[0].headers["traceparent"]) == expected
        assert _trace_id_of(books[0].headers["traceparent"]) == expected
    finally:
        await bus.close()

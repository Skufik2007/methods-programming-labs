"""Подключение к NATS: pull-консьюмер `analyzer` на vitals.raw.> и публикация vitals.stats / vitals.alerts."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import nats
from nats.errors import TimeoutError as NatsTimeout
from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy
from nats.js.errors import NotFoundError

from vitals.common import get_logger
from vitals.streaming.analyzer import Analyzer, alert_message

log = get_logger("vitals.streaming")

STREAM = "VITALS"
DURABLE = "analyzer"


async def run(
    nats_url: str,
    analyzer: Analyzer,
    stop: asyncio.Event,
    stats_interval: float = 2.0,
    batch: int = 256,
) -> None:
    nc = await nats.connect(nats_url, name="vitals-analyzer", max_reconnect_attempts=-1)
    try:
        js = nc.jetstream()
        while not stop.is_set():  # поток создаёт сборщик - ждём его появления
            try:
                await js.stream_info(STREAM)
                break
            except NotFoundError:
                log.info("поток %s ещё не создан, жду", STREAM)
                await asyncio.sleep(1)
        # Новый консьюмер начинает с последних 5 минут (размер окна): старше всё равно было бы «опоздавшим».
        start = (datetime.now(UTC) - timedelta(seconds=analyzer.window.window_s)).isoformat().replace("+00:00", "Z")
        sub = await js.pull_subscribe(
            "vitals.raw.>",
            durable=DURABLE,
            stream=STREAM,
            config=ConsumerConfig(
                ack_policy=AckPolicy.EXPLICIT,
                deliver_policy=DeliverPolicy.BY_START_TIME,
                opt_start_time=start,
                ack_wait=30,
            ),
        )
        ticker = asyncio.create_task(_publisher(nc, analyzer, stop, stats_interval))
        try:
            await _consume(sub, analyzer, stop, batch)
        finally:
            stop.set()
            await ticker
        await nc.flush()
    finally:
        await nc.close()


async def _consume(sub: Any, analyzer: Analyzer, stop: asyncio.Event, batch: int) -> None:
    while not stop.is_set():
        try:
            msgs = await sub.fetch(batch, timeout=1)
        except NatsTimeout:
            continue
        except Exception:  # noqa: BLE001 - переподключение/сбой сервера: логируем и пробуем снова
            log.exception("ошибка fetch")
            await asyncio.sleep(1)
            continue
        for m in msgs:
            try:
                analyzer.ingest(json.loads(m.data))
            except (ValueError, TypeError):
                analyzer.counters["bad_message"] += 1  # битый JSON подтверждаем, иначе он вернётся навсегда
            await m.ack()  # ack - только после обработки


async def _publisher(nc: Any, analyzer: Analyzer, stop: asyncio.Event, interval: float) -> None:
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            pass
        for ev in analyzer.tick():
            await nc.publish("vitals.alerts", json.dumps(alert_message(ev), ensure_ascii=False).encode())
            log.info("тревога", extra={"alert": ev.alert, "patient": ev.patient_id, "state": ev.event})
        await nc.publish("vitals.stats", json.dumps(analyzer.snapshot(), ensure_ascii=False).encode())

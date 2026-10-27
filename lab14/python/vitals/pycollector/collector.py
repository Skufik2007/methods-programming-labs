"""Ядро Python-сборщика: опрос (asyncio) -> очередь -> окна и пакетная запись -> чекпоинты.

Порядок graceful shutdown как в разделе 4: прекратить опрос, дождаться очереди, закрыть окна,
сбросить стоки, записать чекпоинты, закрыть соединения.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web

from vitals.pycollector.config import Config
from vitals.pycollector.model import Agg, Reading, agg_line, reading_from_json, reading_line
from vitals.pycollector.sink import FileCheckpoints, FilesSink
from vitals.pycollector.windows import AddResult, TumblingAggregator

log = logging.getLogger("pycollector")

WINDOW_TICK = 0.5  # как часто проверяются окна, готовые к закрытию (как в Go)
FLUSH_RETRY_DELAY = 1.0


def _wall_ms() -> int:
    return time.time_ns() // 1_000_000


class Collector:
    def __init__(self, cfg: Config, clock_ms: Callable[[], int] = _wall_ms) -> None:
        self.cfg = cfg
        self._clock_ms = clock_ms
        out = Path(cfg.out_dir)
        self.sink = FilesSink(out, cfg.collector_id, lambda: datetime.fromtimestamp(clock_ms() / 1000, UTC))
        self.checkpoints = FileCheckpoints(out / "checkpoints.json")
        self.agg = TumblingAggregator(cfg.collector_id, int(cfg.window * 1000), int(cfg.window_lateness * 1000))
        self.emit_raw = cfg.emit != "agg"
        self.emit_agg = cfg.emit != "raw"

        self._queue: asyncio.Queue[list[dict[str, Any]] | None] = asyncio.Queue()
        self._queued = 0  # показаний в очереди: ограничение BUFFER считаем в показаниях, не в пакетах
        self._room = asyncio.Event()
        self._room.set()
        self._cursors: dict[str, int] = {}  # отправлено в очередь (≠ чекпоинт: тот после записи)
        self._inflight: set[str] = set()
        self._devices: list[str] = list(cfg.device_list)
        self._stop = asyncio.Event()
        self._session: aiohttp.ClientSession | None = None
        self._raw: list[str] = []
        self._agg_lines: list[str] = []
        self._pending: dict[str, int] = {}
        self.m = {"polls": 0, "poll_errors": 0, "readings": 0, "errors": 0, "flushes": 0, "flush_records": 0}
        self.started = 0.0

    def request_stop(self) -> None:
        self._stop.set()

    # ---------- опрос ----------

    async def _list_devices(self) -> list[str]:
        assert self._session is not None
        async with self._session.get(self.cfg.sim_url + "/v1/devices") as resp:
            resp.raise_for_status()
            data = await resp.json()
        return [d["device_id"] for d in data["devices"]]

    async def _enqueue(self, batch: list[dict[str, Any]]) -> None:
        while self._queued >= self.cfg.buffer:
            self._room.clear()
            await self._room.wait()
        self._queued += len(batch)
        self._queue.put_nowait(batch)

    async def _poll_device(self, dev: str) -> None:
        assert self._session is not None
        cur = self._cursors.get(dev)
        if cur is None:
            cur = self._cursors[dev] = self.checkpoints.load(dev)
        while not self._stop.is_set():
            url = f"{self.cfg.sim_url}/v1/devices/{dev}/readings?after_seq={cur}&limit={self.cfg.fetch_limit}"
            try:
                async with self._session.get(url) as resp:
                    resp.raise_for_status()
                    data = await resp.json(loads=json.loads)
            except (aiohttp.ClientError, TimeoutError) as exc:
                self.m["polls"] += 1
                if not self._stop.is_set():
                    self.m["poll_errors"] += 1
                    self.m["errors"] += 1
                    log.warning("ошибка опроса", extra={"device": dev, "err": str(exc)})
                return
            self.m["polls"] += 1
            readings = data["readings"]
            if readings:
                await self._enqueue(readings)
            self.m["readings"] += len(readings)
            prev = cur
            last = int(data.get("last_seq", cur))
            if last > cur:
                cur = self._cursors[dev] = last
            if cur - prev < self.cfg.fetch_limit:
                return  # догнали

    async def _worker(self, jobs: asyncio.Queue[str | None]) -> None:
        while (dev := await jobs.get()) is not None:
            try:
                await self._poll_device(dev)
            finally:
                self._inflight.discard(dev)

    async def _dispatcher(self, jobs: asyncio.Queue[str | None]) -> None:
        while not self._stop.is_set():
            if not self._devices:
                try:
                    self._devices = await self._list_devices()
                except (aiohttp.ClientError, TimeoutError) as exc:
                    self.m["errors"] += 1
                    log.warning("не удалось получить список устройств", extra={"err": str(exc)})
            for dev in self._devices:
                if dev in self._inflight:
                    continue  # предыдущий опрос ещё идёт
                self._inflight.add(dev)
                await jobs.put(dev)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), self.cfg.poll_interval)

    # ---------- конвейер ----------

    async def _pipeline(self) -> None:
        cfg = self.cfg
        loop = asyncio.get_running_loop()
        next_flush = loop.time() + cfg.flush_interval
        next_win = loop.time() + WINDOW_TICK
        while True:
            timeout = max(0.0, min(next_flush, next_win) - loop.time())
            try:
                batch = await asyncio.wait_for(self._queue.get(), timeout)
            except TimeoutError:
                batch = ...  # таймер, не данные
            if batch is None:
                break
            if batch is not ...:
                self._queued -= len(batch)
                self._room.set()
                self._handle(batch)
                if len(self._raw) + len(self._agg_lines) >= cfg.batch_size:
                    await self._flush_safe()
                    next_flush = loop.time() + cfg.flush_interval
            now = loop.time()
            if now >= next_win:
                if self.emit_agg:
                    self._add_agg(self.agg.close_due(self._clock_ms()))
                next_win = now + WINDOW_TICK
            if now >= next_flush:
                await self._flush_safe()
                next_flush = loop.time() + cfg.flush_interval
        # очередь дочитана: шаги 3-5 остановки
        self._finish_windows()
        await self._flush(final=True)

    def _handle(self, batch: list[dict[str, Any]]) -> None:
        now_ms = self._clock_ms()
        pending = self._pending
        for raw in batch:
            r: Reading = reading_from_json(raw)
            if r.seq > pending.get(r.device_id, 0):
                pending[r.device_id] = r.seq
            if self.emit_agg and self.agg.add(r, now_ms) is AddResult.LATE:
                pass  # учтено в agg.stats.late_dropped
            if self.emit_raw:
                self._raw.append(reading_line(r))

    def _add_agg(self, aggs: list[Agg]) -> None:
        self._agg_lines.extend(agg_line(a) for a in aggs)

    def _finish_windows(self) -> None:
        if not self.emit_agg:
            return
        before = self.agg.stats.early_closed
        aggs = self.agg.close_all(self._clock_ms())
        log.info("остановка: шаг 3 — окна закрыты", extra={
            "windows": len(aggs), "early_closed": self.agg.stats.early_closed - before, "reason": "shutdown"})
        self._add_agg(aggs)

    async def _flush_safe(self) -> None:
        try:
            await self._flush(final=False)
        except OSError as exc:  # буфер сохранён, повторим со следующим сбросом
            self.m["errors"] += 1
            log.error("ошибка стока", extra={"sink": "files", "err": str(exc)})

    async def _flush(self, final: bool) -> None:
        if not (self._raw or self._agg_lines or self._pending):
            return
        raw, agg, pending = self._raw, self._agg_lines, self._pending
        total = len(raw) + len(agg)
        # сначала данные и fsync, потом чекпоинт; новые данные идут в свежие списки
        self._raw, self._agg_lines, self._pending = [], [], {}
        try:
            await asyncio.to_thread(self.sink.write_and_sync, "raw", raw)
            raw = []  # уже на диске — при повторе не писать второй раз
            await asyncio.to_thread(self.sink.write_and_sync, "agg", agg)
        except OSError:
            # вернуть незаписанное в начало буфера, ничего не потеряв
            self._raw[:0], self._agg_lines[:0] = raw, agg
            for d, v in pending.items():
                self._pending[d] = max(v, self._pending.get(d, 0))
            self.m["flush_errors"] = self.m.get("flush_errors", 0) + 1
            raise
        self.m["flushes"] += 1
        self.m["flush_records"] += total
        if pending:
            await asyncio.to_thread(self.checkpoints.save, pending)

    # ---------- /metrics, /status ----------

    def _status(self) -> dict[str, Any]:
        s = self.agg.stats
        return {
            "collector_id": self.cfg.collector_id, "uptime_s": round(time.time() - self.started, 3),
            **{f"{k}_total": v for k, v in self.m.items()},
            "queued": self._queued, "devices": len(self._devices),
            "bytes": dict(self.sink.bytes), "records": dict(self.sink.records),
            "windows": {"open": s.open, "closed": s.closed, "early_closed": s.early_closed,
                        "late_dropped": s.late_dropped, "dup_skipped": s.dup_skipped,
                        "future_clamped": s.future_clamped},
        }

    def _web_app(self) -> web.Application:
        async def healthz(_: web.Request) -> web.Response:
            return web.json_response({"status": "ok"})

        async def status(_: web.Request) -> web.Response:
            return web.json_response(self._status())

        async def metrics(_: web.Request) -> web.Response:
            st = self._status()
            lines = [f"collector_{k}_total {v}" for k, v in self.m.items()]
            lines.append(f"collector_late_dropped_total {st['windows']['late_dropped']}")
            lines.append(f"collector_queue_size {st['queued']}")
            return web.Response(text="\n".join(lines) + "\n", content_type="text/plain")

        app = web.Application()
        app.add_routes([web.get("/healthz", healthz), web.get("/status", status), web.get("/metrics", metrics)])
        return app

    # ---------- запуск ----------

    async def run(self, duration: float = 0.0) -> int:
        """Работает до request_stop() / истечения duration (сек, 0 — бесконечно); затем graceful shutdown."""
        cfg = self.cfg
        self.started = time.time()
        runner: web.AppRunner | None = None
        if cfg.status_addr:
            host, _, port = cfg.status_addr.rpartition(":")
            runner = web.AppRunner(self._web_app(), access_log=None)
            await runner.setup()
            await web.TCPSite(runner, host or None, int(port)).start()
        conn = aiohttp.TCPConnector(limit=cfg.workers * 2, keepalive_timeout=60)
        self._session = aiohttp.ClientSession(
            connector=conn, timeout=aiohttp.ClientTimeout(total=cfg.http_timeout))
        log.info("сборщик запущен", extra={"workers": cfg.workers, "emit": cfg.emit, "window": cfg.window})

        jobs: asyncio.Queue[str | None] = asyncio.Queue(maxsize=cfg.workers)
        pipe = asyncio.create_task(self._pipeline())
        workers = [asyncio.create_task(self._worker(jobs)) for _ in range(cfg.workers)]
        disp = asyncio.create_task(self._dispatcher(jobs))

        if duration > 0:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), duration)
        else:
            await self._stop.wait()
        self._stop.set()

        err: BaseException | None = None
        log.info("остановка: шаг 1 — опрос прекращён")
        # диспетчер может висеть на put в полную очередь: дренируем её, пока он не завершится
        while not disp.done():
            with contextlib.suppress(asyncio.QueueEmpty):
                jobs.get_nowait()
                # вытащенное устройство не опрашиваем — снимаем пометку
            await asyncio.sleep(0.01)
        for _ in workers:
            await jobs.put(None)
        try:
            await asyncio.wait_for(asyncio.gather(*workers), cfg.shutdown_timeout)
        except TimeoutError:
            for w in workers:
                w.cancel()
            self.m["errors"] += 1
            log.error("остановка: воркеры не завершились за SHUTDOWN_TIMEOUT")
        log.info("остановка: шаг 2 — ожидание обработки очереди", extra={"queued": self._queued})
        self._queue.put_nowait(None)
        try:
            await pipe
        except OSError as exc:
            err = exc
            log.error("остановка: не удалось сбросить буферы", extra={"err": str(exc)})
        log.info("остановка: шаги 4–5 — стоки сброшены, чекпоинты записаны")
        await asyncio.to_thread(self.sink.close)
        await self._session.close()
        if runner is not None:
            await runner.cleanup()
        log.info("сборщик остановлен", extra={"err": str(err) if err else None})
        return 1 if err else 0

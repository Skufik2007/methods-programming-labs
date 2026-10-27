"""Сценарий transport: JSONL против Arrow Flight на одном наборе (raw и agg).

Go-сборщик с SINKS=files,flight работает в реальном времени; пока он жив, из его файлов читается
JSONL и тем же моментом по DoGet забирается Arrow. Наборы совпадают не до строки (сборщик
продолжает писать), поэтому сравниваются удельные величины: байт/строку и мкс/строку.

  JSONL: «передача» = чтение файлов в память (диск, кэш ОС тёплый), «загрузка» = разбор в Polars.
  Flight: «передача» = DoGet + read_all (loopback gRPC), «загрузка» = Arrow -> Polars.
  Байты Flight = размер IPC-потока полученной таблицы (то, что реально идёт по сети, с учётом словарей).
"""

from __future__ import annotations

import io
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import polars as pl
import pyarrow as pa

from vitals.analytics.load import dictionaries_to_strings, make_ticket
from vitals.analytics.schema import AGG_JSON_SCHEMA, RAW_JSON_SCHEMA
from vitals.bench import procs
from vitals.bench.plots import grouped_bars, panels, save_figure
from vitals.bench.results import IMG_DIR, write_result
from vitals.bench.stats import median, rate, summarize_runs


def ipc_size(table: pa.Table) -> int:
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as w:
        w.write_table(table)
    return sink.getvalue().size


def jsonl_measure(directory: Path, kind: str) -> dict[str, Any]:
    files = sorted(directory.glob("*.jsonl"))
    t0 = time.perf_counter()
    blobs = [f.read_bytes() for f in files]
    t_transfer = time.perf_counter() - t0
    schema = RAW_JSON_SCHEMA if kind == "raw" else AGG_JSON_SCHEMA
    t1 = time.perf_counter()
    df = pl.concat([pl.read_ndjson(io.BytesIO(b), schema=schema) for b in blobs if b])
    if kind == "raw":
        df = df.with_columns(pl.col("ts").str.to_datetime("%Y-%m-%dT%H:%M:%S%.3fZ", time_unit="ms", time_zone="UTC"))
    else:
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        df = df.with_columns(pl.col("window_start", "window_end").str.to_datetime(fmt, time_unit="ms", time_zone="UTC"))
    t_load = time.perf_counter() - t1
    return {"rows": df.height, "bytes": sum(len(b) for b in blobs), "transfer_s": t_transfer, "load_s": t_load}


def flight_measure(addr: str, kind: str) -> dict[str, Any]:
    import pyarrow.flight as flight

    ticket = flight.Ticket(make_ticket(kind))
    with flight.connect(f"grpc://{addr}") as client:
        t0 = time.perf_counter()
        table = client.do_get(ticket).read_all()
        t_transfer = time.perf_counter() - t0
    t1 = time.perf_counter()
    df = pl.from_arrow(dictionaries_to_strings(table))
    t_load = time.perf_counter() - t1
    return {"rows": df.height, "bytes": ipc_size(table), "transfer_s": t_transfer, "load_s": t_load}


def run(quick: bool = False, devices: int | None = None, seconds: float | None = None,
        repeats: int | None = None) -> dict[str, Any]:
    procs.ensure_go_binaries()
    devices = devices or (100 if quick else 300)
    seconds = seconds or (25.0 if quick else 40.0)
    repeats = repeats or 3
    out = Path(tempfile.mkdtemp(prefix="bench-transport-"))
    flight_port = procs.free_port(18815)
    status_port = procs.free_port(19110)
    runs: list[dict[str, Any]] = []
    try:
        with procs.simulator(procs.free_port(18090), devices, 0) as url:
            env = procs.collector_env(url, out, status_port, emit="both", window=10, extra={
                "SINKS": "files,flight", "FLIGHT_ADDR": f"127.0.0.1:{flight_port}", "FLUSH_INTERVAL": "500ms"})
            cmd = procs.collector_command("go", seconds + 20)
            proc = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # noqa: S603
            try:
                time.sleep(seconds)  # накопить данные и закрыть несколько окон
                for rep in range(repeats):
                    for kind in ("raw", "agg"):
                        r = jsonl_measure(out / kind, kind)
                        r.update(kind=kind, format="jsonl", repeat=rep)
                        runs.append(r)
                        r = flight_measure(f"127.0.0.1:{flight_port}", kind)
                        r.update(kind=kind, format="flight", repeat=rep)
                        runs.append(r)
            finally:
                proc.wait(60)
    finally:
        shutil.rmtree(out, ignore_errors=True)
    for r in runs:
        rows = r["rows"]
        r["bytes_per_row"] = r["bytes"] / rows if rows else 0.0
        r["total_us_per_row"] = 1e6 * rate(r["transfer_s"] + r["load_s"], rows) if rows else 0.0
    summary = summarize_runs(runs, ("kind", "format"),
                             ("rows", "bytes", "bytes_per_row", "transfer_s", "load_s", "total_us_per_row"))
    for s in summary:
        print(f"  {s['kind']:4} {s['format']:6}: {s['rows']:.0f} строк, {s['bytes_per_row']:.1f} Б/строку, "
              f"передача {s['transfer_s'] * 1000:.1f} мс, загрузка {s['load_s'] * 1000:.1f} мс", flush=True)
    path = write_result("transport", {"params": {"quick": quick, "devices": devices, "seconds": seconds,
                                                 "repeats": repeats}, "runs": runs, "summary": summary})
    plot(summary)
    print(f"-> {path}")
    return {"summary": summary}


def plot(summary: list[dict[str, Any]]) -> Path:
    def pick(kind: str, fmt: str, key: str) -> float:
        return median(s[key] for s in summary if s["kind"] == kind and s["format"] == fmt)

    labels = ["raw", "agg"]
    fig, axes = panels(3)
    for ax, key, mult, ylabel, title in (
        (axes[0], "bytes_per_row", 1.0, "Б/строку", "Размер на строку"),
        (axes[1], "transfer_s", 1000.0, "мс", "Время передачи"),
        (axes[2], "load_s", 1000.0, "мс", "Время загрузки в Polars"),
    ):
        grouped_bars(ax, labels, {f: [pick(k, f, key) * mult for k in labels] for f in ("jsonl", "flight")},
                     ylabel, title)
    return save_figure(fig, "bench_transport.png", IMG_DIR)

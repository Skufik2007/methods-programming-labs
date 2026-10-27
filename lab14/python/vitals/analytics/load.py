"""Загрузка данных: JSONL (`scan_ndjson`) и Arrow Flight. Сводка набора. Ввод-вывод только здесь."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import polars as pl

from vitals.analytics.schema import AGG_JSON_SCHEMA, RAW_JSON_SCHEMA


def scan_jsonl(directory: str | Path, kind: str) -> pl.LazyFrame | None:
    """Ленивое чтение `<directory>/<kind>/*.jsonl` (kind = raw|agg). None, если файлов нет."""
    folder = Path(directory) / kind
    files = sorted(folder.glob("*.jsonl"))
    if not files:
        return None
    schema = RAW_JSON_SCHEMA if kind == "raw" else AGG_JSON_SCHEMA
    frames = [pl.scan_ndjson(f, schema=schema) for f in files if f.stat().st_size > 0]
    return pl.concat(frames) if frames else None


def make_ticket(kind: str, since: datetime | str | None = None) -> bytes:
    """Тикет DoGet из раздела 6: JSON {"kind": "raw"|"agg", "since": "<RFC 3339>"}."""
    if isinstance(since, datetime):
        since = since.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    return json.dumps({"kind": kind, "since": since or "1970-01-01T00:00:00Z"}).encode()


def dictionaries_to_strings(table: Any) -> Any:
    """Словарные колонки Arrow (dictionary<int32, utf8>) -> utf8, чтобы Polars не делал Categorical."""
    import pyarrow as pa

    fields = []
    for field in table.schema:
        if pa.types.is_dictionary(field.type):
            fields.append(field.with_type(field.type.value_type))
        else:
            fields.append(field)
    return table.cast(pa.schema(fields))


def fetch_flight(addresses: Sequence[str], kind: str, since: datetime | str | None = None) -> pl.DataFrame:
    """DoGet на каждом сервере -> один DataFrame (concat). Недоступный сервер — исключение с адресом."""
    import pyarrow.flight as flight

    ticket = flight.Ticket(make_ticket(kind, since))
    frames: list[pl.DataFrame] = []
    for addr in addresses:
        location = addr if "://" in addr else f"grpc://{addr}"
        try:
            with flight.connect(location) as client:
                table = client.do_get(ticket).read_all()
        except flight.FlightError as exc:
            raise RuntimeError(f"Flight-сервер {location} недоступен: {exc}") from exc
        frames.append(pl.from_arrow(dictionaries_to_strings(table)))  # type: ignore[arg-type]
    if not frames:
        raise ValueError("не указано ни одного Flight-сервера")
    return pl.concat(frames, how="diagonal_relaxed")


def summarize(frame: pl.DataFrame | pl.LazyFrame, head: int = 5) -> dict[str, Any]:
    """Сводка набора: head, схема, число строк, пропуски по столбцам."""
    df = frame.collect() if isinstance(frame, pl.LazyFrame) else frame
    return {
        "rows": df.height,
        "schema": {name: str(dtype) for name, dtype in df.schema.items()},
        "nulls": {name: int(n) for name, n in zip(df.columns, df.null_count().row(0), strict=True)},
        "head": df.head(head).to_dicts(),
    }


def format_summary(name: str, summary: dict[str, Any]) -> str:
    """Человекочитаемый вывод сводки (для консоли)."""
    lines = [f"== {name}: {summary['rows']} строк ==", "схема:"]
    lines += [f"  {k}: {v}" for k, v in summary["schema"].items()]
    lines.append("пропуски: " + ", ".join(f"{k}={v}" for k, v in summary["nulls"].items()))
    lines.append("head:")
    lines += [f"  {row}" for row in summary["head"]]
    return "\n".join(lines)



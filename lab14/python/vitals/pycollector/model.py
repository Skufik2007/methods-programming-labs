"""Модель показания и сериализация JSONL, совпадающая с Go (model.AppendJSON) по байтам.

Поля идут в порядке контракта, без пробелов, float всегда с точкой (72.0), ts — миллисекунды + Z.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from json.encoder import encode_basestring as _quote  # без ensure_ascii: кириллица как есть, как в Go
from typing import Any, NamedTuple

RANGES: dict[str, tuple[float, float, str]] = {
    "heart_rate": (25.0, 250.0, "bpm"),
    "spo2": (50.0, 100.0, "%"),
    "temperature": (30.0, 43.0, "C"),
    "systolic_bp": (50.0, 260.0, "mmHg"),
    "diastolic_bp": (30.0, 160.0, "mmHg"),
    "resp_rate": (4.0, 60.0, "breaths/min"),
    "steps": (0.0, 400.0, "count"),
}

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MS = timedelta(milliseconds=1)


class Reading(NamedTuple):
    device_id: str
    patient_id: str
    seq: int
    ts_ms: int  # мс эпохи
    metric: str
    value: float | None
    unit: str
    battery: int | None
    ts_text: str  # уже готовая каноническая строка ts


def parse_ts_ms(text: str) -> int:
    """RFC 3339 -> мс эпохи (точная целочисленная арифметика, без float)."""
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return (dt - _EPOCH) // _MS


def format_ts(ms: int) -> str:
    # усечение до миллисекунд, как time.Format в Go
    dt = _EPOCH + timedelta(milliseconds=ms)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def reading_from_json(d: dict[str, Any]) -> Reading:
    ts = d["ts"]
    ms = parse_ts_ms(ts)
    # типичный ответ эмулятора уже в каноническом виде — не форматируем заново
    text = ts if len(ts) == 24 and ts.endswith("Z") else format_ts(ms)
    v = d.get("value")
    b = d.get("battery")
    return Reading(
        d["device_id"], d["patient_id"], int(d["seq"]), ms, d["metric"],
        None if v is None else float(v), d.get("unit", ""), None if b is None else int(b), text,
    )


def valid_value(metric: str, v: float | None) -> bool:
    if v is None or math.isnan(v) or math.isinf(v):
        return False
    r = RANGES.get(metric)
    return r is not None and r[0] <= v <= r[1]


def fmt_float(v: float) -> str:
    """Как appendFloat в Go: кратчайшее представление, без экспоненты, целые с «.0»."""
    s = repr(v)
    if "e" in s or "E" in s:
        s = format(Decimal(s), "f")
    return s if "." in s else s + ".0"


def _opt_float(v: float | None) -> str:
    return "null" if v is None or math.isnan(v) or math.isinf(v) else fmt_float(v)


def reading_line(r: Reading) -> str:
    b = "null" if r.battery is None else str(r.battery)
    return (
        f'{{"device_id":{_quote(r.device_id)},"patient_id":{_quote(r.patient_id)},"seq":{r.seq},'
        f'"ts":"{r.ts_text}","metric":{_quote(r.metric)},"value":{_opt_float(r.value)},'
        f'"unit":{_quote(r.unit)},"battery":{b}}}\n'
    )


class Agg(NamedTuple):
    window_start_ms: int
    window_end_ms: int
    device_id: str
    patient_id: str
    metric: str
    count: int
    invalid: int
    sum: float
    min: float
    max: float
    last: float
    collector_id: str


def _win_ts(ms: int) -> str:
    return (_EPOCH + timedelta(milliseconds=ms)).strftime("%Y-%m-%dT%H:%M:%SZ")


def agg_line(a: Agg) -> str:
    if a.count == 0:
        # при пустом окне sum = 0.0, остальное null (как в Go)
        tail = '"sum":0.0,"min":null,"max":null,"mean":null,"last":null'
    else:
        tail = (
            f'"sum":{fmt_float(a.sum)},"min":{fmt_float(a.min)},"max":{fmt_float(a.max)},'
            f'"mean":{fmt_float(a.sum / a.count)},"last":{fmt_float(a.last)}'
        )
    return (
        f'{{"window_start":"{_win_ts(a.window_start_ms)}","window_end":"{_win_ts(a.window_end_ms)}",'
        f'"device_id":{_quote(a.device_id)},"patient_id":{_quote(a.patient_id)},"metric":{_quote(a.metric)},'
        f'"count":{a.count},"invalid":{a.invalid},{tail},"collector_id":{_quote(a.collector_id)}}}\n'
    )

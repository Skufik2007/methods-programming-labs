"""Константы предметной области: метрики, единицы, схемы и пороги правил (разделы 2, 6, 7 контракта)."""

from __future__ import annotations

import polars as pl

# metric -> (минимум, максимум, единица). Должно совпадать с таблицей раздела 2 и Rust-валидатором.
METRICS: dict[str, tuple[float, float, str]] = {
    "heart_rate": (25.0, 250.0, "bpm"),
    "spo2": (50.0, 100.0, "%"),
    "temperature": (30.0, 43.0, "C"),
    "systolic_bp": (50.0, 260.0, "mmHg"),
    "diastolic_bp": (30.0, 160.0, "mmHg"),
    "resp_rate": (4.0, 60.0, "breaths/min"),
    "steps": (0.0, 400.0, "count"),
}
CANONICAL_UNIT: dict[str, str] = {m: u for m, (_, _, u) in METRICS.items()}

TS_DTYPE = pl.Datetime("ms", "UTC")

# Схема JSONL-строки raw: ts читаем строкой и разбираем явно (так видны нераспознанные значения).
RAW_JSON_SCHEMA: dict[str, pl.DataType] = {
    "device_id": pl.String(),
    "patient_id": pl.String(),
    "seq": pl.Int64(),
    "ts": pl.String(),
    "metric": pl.String(),
    "value": pl.Float64(),
    "unit": pl.String(),
    "battery": pl.Int64(),
}
RAW_COLUMNS = list(RAW_JSON_SCHEMA)

AGG_JSON_SCHEMA: dict[str, pl.DataType] = {
    "window_start": pl.String(),
    "window_end": pl.String(),
    "device_id": pl.String(),
    "patient_id": pl.String(),
    "metric": pl.String(),
    "count": pl.Int64(),
    "invalid": pl.Int64(),
    "sum": pl.Float64(),
    "min": pl.Float64(),
    "max": pl.Float64(),
    "mean": pl.Float64(),
    "last": pl.Float64(),
    "collector_id": pl.String(),
}

# Правила эпизодов (раздел 7 / 2.3): (имя, метрика, величина за минуту, оператор, порог).
EPISODE_RULES: tuple[tuple[str, str, str, str, float], ...] = (
    ("tachycardia", "heart_rate", "mean", ">", 120.0),
    ("desaturation", "spo2", "min", "<", 92.0),
    ("fever", "temperature", "last", ">", 38.0),
    ("hypertension", "systolic_bp", "mean5", ">", 160.0),  # среднее САД за 5 минут
)

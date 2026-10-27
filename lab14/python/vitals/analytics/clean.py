"""Очистка raw-показаний. Каждый шаг — чистая функция DataFrame -> (DataFrame, запись отчёта).

Запись отчёта: {"step", "rows_before", "rows_after", "affected", "note"}.
`affected` — сколько строк затронул шаг (удалено или исправлено); для шагов-исправлений
(unit, battery) `rows_after == rows_before`.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any

import polars as pl

from vitals.analytics.schema import CANONICAL_UNIT, METRICS, RAW_COLUMNS, TS_DTYPE

Report = dict[str, Any]
Step = Callable[[pl.DataFrame], tuple[pl.DataFrame, Report]]

# Коды причин валидации (раздел 10).
REASON_OK, REASON_NULL, REASON_RANGE, REASON_METRIC, REASON_UNIT = 0, 1, 2, 3, 4
REASON_NAMES = {1: "null", 2: "out_of_range", 3: "unknown_metric", 4: "bad_unit"}

try:  # модуль пишет другой генератор; без него работает запасная реализация ниже
    from vitals.validation import validate as _validate
except ImportError:  # pragma: no cover - зависит от окружения
    _validate = None


def _entry(step: str, before: int, after: int, affected: int, note: str) -> Report:
    return {"step": step, "rows_before": before, "rows_after": after, "affected": affected, "note": note}


def validate_fallback(df: pl.DataFrame) -> pl.Series:
    """Запасная валидация с тем же контрактом (приоритет кодов 3 -> 1 -> 2 -> 4)."""
    lo = {m: r[0] for m, r in METRICS.items()}
    hi = {m: r[1] for m, r in METRICS.items()}
    metric = pl.col("metric")
    value = pl.col("value")
    code = (
        pl.when(~metric.is_in(list(METRICS)) | metric.is_null())
        .then(REASON_METRIC)
        .when(value.is_null() | value.is_nan())
        .then(REASON_NULL)
        .when((value < metric.replace_strict(lo, default=None)) | (value > metric.replace_strict(hi, default=None)))
        .then(REASON_RANGE)
        .when(pl.col("unit").is_null() | (pl.col("unit") != metric.replace_strict(CANONICAL_UNIT, default=None)))
        .then(REASON_UNIT)
        .otherwise(REASON_OK)
        .cast(pl.UInt8)
        .alias("reason")
    )
    return df.select(code).to_series()


def validation_codes(df: pl.DataFrame) -> pl.Series:
    fn = _validate if _validate is not None else validate_fallback
    return fn(df.select("metric", "value", "unit"))


def coerce_types(df: pl.DataFrame) -> tuple[pl.DataFrame, Report]:
    """Шаг 1: типы. ts (строка RFC 3339 или datetime) -> Datetime[ms, UTC]; seq/battery -> int; value -> float.

    Строки с нераспознанным ts/seq/device_id удаляются: без них нельзя ни дедуплицировать, ни упорядочить.
    Шаг стоит первым, потому что остальные сравнивают ts как время.
    """
    before = df.height
    if df.schema["ts"] == pl.String:
        ts_expr = pl.col("ts").str.to_datetime(time_unit="ms", time_zone="UTC", strict=False)
    else:
        ts_expr = pl.col("ts").cast(TS_DTYPE)
    typed = df.select(
        pl.col("device_id").cast(pl.String),
        pl.col("patient_id").cast(pl.String),
        pl.col("seq").cast(pl.Int64, strict=False),
        ts_expr.alias("ts"),
        pl.col("metric").cast(pl.String),
        pl.col("value").cast(pl.Float64, strict=False),
        pl.col("unit").cast(pl.String),
        pl.col("battery").cast(pl.Int64, strict=False),
    ).select(RAW_COLUMNS)
    out = typed.filter(pl.col("ts").is_not_null() & pl.col("seq").is_not_null() & pl.col("device_id").is_not_null())
    note = "удалены строки с нераспознанными ts/seq/device_id"
    return out, _entry("coerce_types", before, out.height, before - out.height, note)


def drop_duplicates(df: pl.DataFrame) -> tuple[pl.DataFrame, Report]:
    """Шаг 2: дубликаты по (device_id, seq) — остаётся первая строка (порядок сохраняется)."""
    before = df.height
    out = df.unique(subset=["device_id", "seq"], keep="first", maintain_order=True)
    return out, _entry("drop_duplicates", before, out.height, before - out.height, "ключ (device_id, seq), keep=first")


def normalize_unit(df: pl.DataFrame) -> tuple[pl.DataFrame, Report]:
    """Шаг 3: единица приводится к канонической для метрики, если отличается только регистром/пробелами/пустая.

    Действительно чужая единица (например "kg") не трогается — её отсеет валидация (код 4).
    """
    canonical = pl.col("metric").replace_strict(CANONICAL_UNIT, default=None)
    cleaned = pl.col("unit").str.strip_chars()
    fixable = canonical.is_not_null() & (
        cleaned.is_null() | (cleaned == "") | (cleaned.str.to_lowercase() == canonical.str.to_lowercase())
    )
    changed = fixable & (pl.col("unit").is_null() | (pl.col("unit") != canonical))
    affected = int(df.select(changed.sum()).item())
    out = df.with_columns(pl.when(fixable).then(canonical).otherwise(pl.col("unit")).alias("unit"))
    note = "регистр/пробелы/пустая единица -> каноническая"
    return out, _entry("normalize_unit", df.height, out.height, affected, note)


def drop_future(df: pl.DataFrame, tolerance: timedelta = timedelta(seconds=5)) -> tuple[pl.DataFrame, Report]:
    """Шаг 4: удалить показания с ts из будущего (сбой часов устройства).

    Простое «ts > max(ts) + допуск» не годится: будущий ts сам становится максимумом набора. Поэтому два признака:
    1) внутри устройства seq растёт вместе с ts, значит ts строки не может быть больше минимального ts более
       поздних seq этого устройства (больше допуска);
    2) для последней строки устройства (сравнивать не с чем) — ts больше максимума остальных строк + допуск.
    """
    before = df.height
    if before == 0:
        return df, _entry("drop_future", 0, 0, 0, "пустой набор")
    tol_ms = int(tolerance.total_seconds() * 1000)
    work = df.with_row_index("_i").sort("device_id", "seq").with_columns(
        pl.col("ts").dt.epoch("ms").alias("_ms"),
    )
    work = work.with_columns(
        # минимум ts среди более поздних seq того же устройства: суффиксный cum_min со сдвигом на строку
        pl.col("_ms").reverse().cum_min().reverse().shift(-1).over("device_id").alias("_later_min"),
    )
    has_later = pl.col("_later_min").is_not_null()
    broken = has_later & (pl.col("_ms") - pl.col("_later_min") > tol_ms)
    reference = work.filter(~broken)["_ms"].max()  # максимум по строкам, не уличённым признаком 1
    # у последних строк устройств сравнивать не с чем: берём максимум по строкам с «поздними» соседями
    anchor = work.filter(has_later & ~broken)["_ms"].max()
    limit = (anchor if anchor is not None else reference) + tol_ms
    tail_future = ~has_later & (pl.col("_ms") > limit)
    out = work.filter(~(broken | tail_future)).sort("_i").drop("_i", "_ms", "_later_min")
    note = f"ts нарушает монотонность seq или больше максимума набора (допуск {tolerance.total_seconds():g} с)"
    return out, _entry("drop_future", before, out.height, before - out.height, note)


def drop_invalid(df: pl.DataFrame) -> tuple[pl.DataFrame, Report]:
    """Шаг 5: валидация (vitals.validation.validate): удаляются коды 2 (диапазон), 3 (метрика), 4 (единица).

    Код 1 (пропуск value) оставляем шагу drop_missing, чтобы число пропусков считалось отдельно.
    """
    before = df.height
    if before == 0:
        return df, _entry("validate", 0, 0, 0, "пустой набор")
    codes = validation_codes(df)
    bad = codes.is_in([REASON_RANGE, REASON_METRIC, REASON_UNIT])
    by_reason = {REASON_NAMES[c]: int((codes == c).sum()) for c in (REASON_RANGE, REASON_METRIC, REASON_UNIT)}
    out = df.filter(~bad)
    engine = "vitals.validation" if _validate is not None else "запасная реализация (vitals.validation не найден)"
    entry = _entry("validate", before, out.height, before - out.height, f"{engine}; по причинам: {by_reason}")
    entry["detail"] = by_reason  # единственный шаг с доп. полем: разбивка по причинам для графика дефектов
    return out, entry


def drop_missing(df: pl.DataFrame) -> tuple[pl.DataFrame, Report]:
    """Шаг 6: удалить строки без value (null/NaN)."""
    before = df.height
    out = df.filter(pl.col("value").is_not_null() & pl.col("value").is_not_nan())
    return out, _entry("drop_missing", before, out.height, before - out.height, "value is null/NaN")


def fill_battery(df: pl.DataFrame) -> tuple[pl.DataFrame, Report]:
    """Шаг 7: пропуски battery — последним известным значением по устройству (по seq); ведущие пропуски остаются."""
    nulls_before = int(df["battery"].null_count())
    out = (
        df.sort("device_id", "seq")
        .with_columns(pl.col("battery").forward_fill().over("device_id"))
        .sort("ts", "device_id", "seq")
    )
    left = int(out["battery"].null_count())
    note = f"forward_fill по устройству; осталось пропусков: {left}"
    return out, _entry("fill_battery", df.height, out.height, nulls_before - left, note)


STEPS: tuple[Step, ...] = (
    coerce_types,
    drop_duplicates,
    normalize_unit,
    drop_future,
    drop_invalid,
    drop_missing,
    fill_battery,
)


def clean(df: pl.DataFrame, steps: tuple[Step, ...] = STEPS) -> tuple[pl.DataFrame, list[Report]]:
    """Применить шаги по порядку; вернуть очищенный набор и отчёт по шагам."""
    report: list[Report] = []
    for step in steps:
        df, entry = step(df)
        report.append(entry)
    return df, report

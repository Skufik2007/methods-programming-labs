"""Валидация показаний датчиков (контракт: docs/ARCHITECTURE.md, разделы 2 и 10).

Две реализации с одним контрактом:
  * validate_rust   — модуль vitals_validate (Rust/PyO3), если он собран;
  * validate_polars — чистая реализация на выражениях Polars (запасная и для сравнения скорости).
`validate(df)` берёт Rust, если модуль доступен, иначе Polars.

Коды: 0 ок, 1 пропуск/NaN, 2 вне диапазона, 3 неизвестная метрика, 4 неверная единица.
Приоритет при нескольких нарушениях: 3 -> 1 -> 2 -> 4.
"""
from __future__ import annotations

import numpy as np
import polars as pl
import pyarrow as pa
import pyarrow.compute as pc

try:
    import vitals_validate as _rust
except ImportError:  # модуль не собран — работаем на Polars
    _rust = None

# Таблица диапазонов: метрика -> (мин, макс, единица), границы включительно.
# ВАЖНО: должна совпадать с RANGES в rust/vitals_validate/src/lib.rs
# (совпадение проверяет тест). Если модуль собран, берём таблицу из него.
_RANGES_FALLBACK: dict[str, tuple[float, float, str]] = {
    "heart_rate": (25.0, 250.0, "bpm"),
    "spo2": (50.0, 100.0, "%"),
    "temperature": (30.0, 43.0, "C"),
    "systolic_bp": (50.0, 260.0, "mmHg"),
    "diastolic_bp": (30.0, 160.0, "mmHg"),
    "resp_rate": (4.0, 60.0, "breaths/min"),
    "steps": (0.0, 400.0, "count"),
}

RANGES: dict[str, tuple[float, float, str]] = (
    dict(_rust.ranges()) if _rust is not None else dict(_RANGES_FALLBACK)
)

OK, MISSING, OUT_OF_RANGE, UNKNOWN_METRIC, BAD_UNIT = 0, 1, 2, 3, 4


def has_rust() -> bool:
    return _rust is not None


def validate_rust(df: pl.DataFrame) -> pl.Series:
    """Валидация через Rust. Бросает RuntimeError, если модуль не собран."""
    if _rust is None:
        raise RuntimeError("модуль vitals_validate не собран (maturin develop --release)")
    # null -> NaN (в Rust пропуск кодируется NaN); массив без копии, если null нет
    value = df["value"].cast(pl.Float64).fill_null(float("nan")).to_numpy()
    value = np.ascontiguousarray(value, dtype=np.float64)
    metric_codes, metric_dict = _dictionary(df["metric"])
    unit_codes, unit_dict = _dictionary(df["unit"])
    codes = _rust.validate_coded(metric_codes, metric_dict, value, unit_codes, unit_dict)
    return pl.Series("validation", codes, dtype=pl.UInt8)


def _dictionary(column: pl.Series) -> tuple[np.ndarray, list[str | None]]:
    """Столбец строк -> (коды int32, словарь уникальных значений); null -> код -1.

    Так в Rust уходит массив чисел без копии вместо миллиона строк Python: перевод столбца в
    список (to_list) стоил дороже самой проверки, и Rust проигрывал чистому Polars.
    """
    encoded = pc.dictionary_encode(column.cast(pl.String).to_arrow())
    if isinstance(encoded, pa.ChunkedArray):
        encoded = encoded.combine_chunks()
    codes = pc.fill_null(encoded.indices.cast(pa.int32()), -1).to_numpy(zero_copy_only=False)
    return np.ascontiguousarray(codes, dtype=np.int32), encoded.dictionary.to_pylist()


def validate_polars(df: pl.DataFrame) -> pl.Series:
    """Чистая реализация на выражениях Polars, тот же контракт."""
    metric, value, unit = pl.col("metric"), pl.col("value").cast(pl.Float64), pl.col("unit")

    def pick(idx: int, dtype):
        # цепочка when/then по таблице; для неизвестной метрики остаётся null
        expr = pl.when(pl.lit(False)).then(pl.lit(None, dtype=dtype))
        for name, rec in RANGES.items():
            expr = expr.when(metric == name).then(pl.lit(rec[idx], dtype=dtype))
        return expr.otherwise(pl.lit(None, dtype=dtype))

    lo, hi, exp_unit = pick(0, pl.Float64), pick(1, pl.Float64), pick(2, pl.String)

    unknown = metric.is_in(list(RANGES)).fill_null(False).not_()
    missing = value.is_null() | value.is_nan()
    # NaN и null сюда не доходят (отсечены выше); inf уходит в «вне диапазона»
    out_of_range = ((value >= lo) & (value <= hi)).fill_null(False).not_()
    bad_unit = (unit == exp_unit).fill_null(False).not_()

    # when/then вычисляются по порядку -> первый сработавший = высший приоритет
    code = (
        pl.when(unknown).then(pl.lit(UNKNOWN_METRIC, dtype=pl.UInt8))
        .when(missing).then(pl.lit(MISSING, dtype=pl.UInt8))
        .when(out_of_range).then(pl.lit(OUT_OF_RANGE, dtype=pl.UInt8))
        .when(bad_unit).then(pl.lit(BAD_UNIT, dtype=pl.UInt8))
        .otherwise(pl.lit(OK, dtype=pl.UInt8))
        .alias("validation")
    )
    return df.select(code).to_series()


def validate(df: pl.DataFrame) -> pl.Series:
    """validate(df) -> Series[uint8] с колонками metric, value, unit на входе."""
    return validate_rust(df) if _rust is not None else validate_polars(df)

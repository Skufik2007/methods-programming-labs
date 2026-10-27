"""Одинаковый анализ на DuckDB (SQL к Parquet) и на Polars (scan_parquet) с замером времени.

Оба движка каждый раз читают Parquet заново, поэтому сравнение включает чтение, а не только вычисление.
"""

from __future__ import annotations

import math
import statistics
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import duckdb
import polars as pl

WINDOW_ROWS = 30  # окно скользящего среднего ЧСС, строк


def parquet_glob(clean_dir: str | Path) -> str:
    return (Path(clean_dir) / "*" / "*.parquet").as_posix()


def _scan(clean_dir: str | Path) -> pl.LazyFrame:
    # metric берётся из имени каталога metric=<...> (Hive-партиционирование)
    return pl.scan_parquet(parquet_glob(clean_dir), hive_partitioning=True)


# --- Polars --------------------------------------------------------------------------------------------------------


def _pl_filter(lf: pl.LazyFrame) -> pl.DataFrame:
    return (
        lf.filter((pl.col("metric") == "heart_rate") & (pl.col("value") > 85))
        .select(pl.len().alias("n"), pl.col("value").mean().alias("mean_value"))
        .collect()
    )


def _pl_group(lf: pl.LazyFrame) -> pl.DataFrame:
    return (
        lf.group_by("patient_id", "metric")
        .agg(
            pl.len().alias("n"),
            pl.col("value").sum().alias("total"),
            pl.col("value").mean().alias("mean_value"),
            pl.col("value").min().alias("min_value"),
            pl.col("value").max().alias("max_value"),
        )
        .sort("patient_id", "metric")
        .collect()
    )


def _pl_top(lf: pl.LazyFrame) -> pl.DataFrame:
    return (
        lf.filter(pl.col("metric") == "heart_rate")
        .group_by("patient_id")
        .agg(pl.len().alias("n"), pl.col("value").mean().alias("mean_hr"))
        .filter(pl.col("n") >= 10)
        .sort(["mean_hr", "patient_id"], descending=[True, False])
        .head(10)
        .collect()
    )


def _pl_window(lf: pl.LazyFrame) -> pl.DataFrame:
    return (
        lf.filter(pl.col("metric") == "heart_rate")
        .sort("device_id", "ts", "seq")
        .with_columns(pl.col("value").rolling_mean(WINDOW_ROWS, min_samples=1).over("device_id").alias("ma"))
        .group_by("device_id")
        .agg(pl.col("ma").max().alias("max_ma"))
        .sort("device_id")
        .collect()
    )


# --- DuckDB --------------------------------------------------------------------------------------------------------

SQL: dict[str, str] = {
    "filter": """
        SELECT count(*) AS n, avg(value) AS mean_value
        FROM {src} WHERE metric = 'heart_rate' AND value > 85""",
    "group": """
        SELECT patient_id, metric, count(*) AS n, sum(value) AS total, avg(value) AS mean_value,
               min(value) AS min_value, max(value) AS max_value
        FROM {src} GROUP BY patient_id, metric ORDER BY patient_id, metric""",
    "top": """
        SELECT patient_id, count(*) AS n, avg(value) AS mean_hr
        FROM {src} WHERE metric = 'heart_rate'
        GROUP BY patient_id HAVING count(*) >= 10
        ORDER BY mean_hr DESC, patient_id LIMIT 10""",
    "window": f"""
        WITH r AS (
            SELECT device_id,
                   avg(value) OVER (PARTITION BY device_id ORDER BY ts, seq
                                    ROWS BETWEEN {WINDOW_ROWS - 1} PRECEDING AND CURRENT ROW) AS ma
            FROM {{src}} WHERE metric = 'heart_rate')
        SELECT device_id, max(ma) AS max_ma FROM r GROUP BY device_id ORDER BY device_id""",  # noqa: S608 - подставляется только константа WINDOW_ROWS
}

POLARS: dict[str, Callable[[pl.LazyFrame], pl.DataFrame]] = {
    "filter": _pl_filter,
    "group": _pl_group,
    "top": _pl_top,
    "window": _pl_window,
}


def run_duckdb(con: duckdb.DuckDBPyConnection, name: str, clean_dir: str | Path) -> pl.DataFrame:
    src = f"read_parquet('{parquet_glob(clean_dir)}', hive_partitioning = true)"
    return con.execute(SQL[name].format(src=src)).pl()


def run_polars(name: str, clean_dir: str | Path) -> pl.DataFrame:
    return POLARS[name](_scan(clean_dir))


def frames_match(a: pl.DataFrame, b: pl.DataFrame, rel_tol: float = 1e-9) -> bool:
    """Совпадение результатов: те же столбцы и строки; числа сравниваются с допуском (порядок сумм разный)."""
    if a.columns != b.columns or a.height != b.height:
        return False
    for col in a.columns:
        for x, y in zip(a[col].to_list(), b[col].to_list(), strict=True):
            if isinstance(x, float) or isinstance(y, float):
                if x is None or y is None:
                    if x is not y:
                        return False
                elif not math.isclose(x, y, rel_tol=rel_tol, abs_tol=1e-9):
                    return False
            elif x != y:
                return False
    return True


def _median_seconds(fn: Callable[[], Any], repeats: int) -> tuple[float, list[float]]:
    runs: list[float] = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        fn()
        runs.append(time.perf_counter() - t0)
    return statistics.median(runs), runs


def benchmark(clean_dir: str | Path, repeats: int = 5) -> dict[str, Any]:
    """Прогнать каждый запрос в обоих движках `repeats` раз; вернуть медианы и признак совпадения результатов.

    Первый прогон (прогрев кэшей файловой системы) делается отдельно и в медиану не входит.
    """
    con = duckdb.connect()
    result: dict[str, Any] = {"repeats": repeats, "queries": {}}
    try:
        for name in SQL:
            duck_df = run_duckdb(con, name, clean_dir)
            pl_df = run_polars(name, clean_dir)
            match = frames_match(duck_df, pl_df)
            duck_med, duck_runs = _median_seconds(lambda n=name: run_duckdb(con, n, clean_dir), repeats)
            pl_med, pl_runs = _median_seconds(lambda n=name: run_polars(n, clean_dir), repeats)
            result["queries"][name] = {
                "rows": pl_df.height,
                "match": match,
                "duckdb_median_s": duck_med,
                "polars_median_s": pl_med,
                "duckdb_runs_s": duck_runs,
                "polars_runs_s": pl_runs,
            }
    finally:
        con.close()
    result["all_match"] = all(q["match"] for q in result["queries"].values())
    return result

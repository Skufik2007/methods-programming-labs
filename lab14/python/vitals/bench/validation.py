"""Сценарий validation: Rust-валидатор против чистого Polars на 1 млн строк."""

from __future__ import annotations

import time
from typing import Any

import numpy as np
import polars as pl

from vitals import validation as V
from vitals.bench.plots import grouped_bars, panels, save_figure
from vitals.bench.results import IMG_DIR, write_result
from vitals.bench.stats import median, speedup


def make_frame(rows: int, seed: int = 7) -> pl.DataFrame:
    """Синтетические показания с дефектами: вне диапазона, null, неверная единица, неизвестная метрика."""
    rng = np.random.default_rng(seed)
    names = list(V.RANGES)
    idx = rng.integers(0, len(names), rows)
    lo = np.array([V.RANGES[n][0] for n in names])[idx]
    hi = np.array([V.RANGES[n][1] for n in names])[idx]
    value = lo + (hi - lo) * rng.random(rows)
    far = rng.random(rows) < 0.02
    value[far] = hi[far] * 10  # выход за диапазон
    value[rng.random(rows) < 0.02] = np.nan
    units = np.array([V.RANGES[n][2] for n in names], dtype=object)[idx]
    units[rng.random(rows) < 0.02] = "BPM"
    metric = np.array(names, dtype=object)[idx]
    metric[rng.random(rows) < 0.005] = "unknown_metric"
    return pl.DataFrame({"metric": metric, "value": value, "unit": units}).with_columns(
        pl.when(pl.col("value").is_nan()).then(None).otherwise(pl.col("value")).alias("value"))


def timeit(fn, df: pl.DataFrame, repeats: int) -> tuple[float, pl.Series]:
    times, out = [], None
    for _ in range(repeats):
        t = time.perf_counter()
        out = fn(df)
        times.append(time.perf_counter() - t)
    assert out is not None
    return median(times), out


def run(quick: bool = False, rows: int = 1_000_000, repeats: int | None = None) -> dict[str, Any]:
    repeats = repeats or (3 if quick else 7)
    df = make_frame(rows)
    res: dict[str, Any] = {"rows": rows, "repeats": repeats, "rust_available": V.has_rust()}
    t_polars, codes_polars = timeit(V.validate_polars, df, repeats)
    res["polars_s"] = t_polars
    if V.has_rust():
        V.validate_rust(df.head(1000))  # прогрев (первый вызов, ленивые инициализации)
        t_rust, codes_rust = timeit(V.validate_rust, df, repeats)
        res.update(rust_s=t_rust, speedup=speedup(t_polars, t_rust),
                   results_equal=bool((codes_rust == codes_polars).all()))
    res["code_counts"] = {str(k): int(v) for k, v in
                          codes_polars.value_counts().sort("validation").iter_rows()}
    print(f"  {rows} строк: Polars {t_polars * 1000:.0f} мс"
          + (f", Rust {res['rust_s'] * 1000:.0f} мс (x{res['speedup']:.2f}), равны={res['results_equal']}"
             if "rust_s" in res else ", Rust не собран"), flush=True)
    path = write_result("validation", res)
    plot(res)
    print(f"-> {path}")
    return res


def plot(res: dict[str, Any]) -> None:
    fig, axes = panels(1, 5.5)
    cats, vals = ["Polars"], [res["polars_s"] * 1000]
    if "rust_s" in res:
        cats.append("Rust")
        vals.append(res["rust_s"] * 1000)
    grouped_bars(axes[0], [f"{res['rows']:,} строк"], {c.lower(): [v] for c, v in zip(cats, vals, strict=True)},
                 "мс (медиана)", "Валидация: Rust и Polars")
    save_figure(fig, "bench_validation.png", IMG_DIR)

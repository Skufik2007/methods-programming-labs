"""Rust-валидатор и чистая реализация на Polars обязаны давать одинаковые коды (раздел 10)."""

import numpy as np
import polars as pl
import pytest
from vitals.validation import RANGES, has_rust, validate_polars, validate_rust

pytestmark = pytest.mark.skipif(not has_rust(), reason="модуль vitals_validate не собран")

METRICS = [*RANGES, "unknown", None]
UNITS = [u for (_, _, u) in RANGES.values()] + ["", "BPM", None]


def _frame(n: int, seed: int) -> pl.DataFrame:
    rng = np.random.default_rng(seed)
    value = rng.normal(80, 60, n)
    value[rng.random(n) < 0.05] = np.nan
    value[rng.random(n) < 0.02] = 999.0
    bounds = [b for (lo, hi, _) in RANGES.values() for b in (lo, hi)]  # границы включительно
    edge = rng.random(n) < 0.05
    value[edge] = rng.choice(bounds, edge.sum())
    return pl.DataFrame(
        {
            "metric": [METRICS[i] for i in rng.integers(0, len(METRICS), n)],
            "value": value,
            "unit": [UNITS[i] for i in rng.integers(0, len(UNITS), n)],
        }
    )


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_rust_and_polars_agree(seed: int) -> None:
    df = _frame(50_000, seed)
    assert validate_rust(df).to_list() == validate_polars(df).to_list()


def test_rust_handles_null_strings_and_single_chunk() -> None:
    df = pl.DataFrame({"metric": [None, "spo2"], "value": [1.0, 97.0], "unit": ["%", None]})
    assert validate_rust(df).to_list() == validate_polars(df).to_list() == [3, 4]

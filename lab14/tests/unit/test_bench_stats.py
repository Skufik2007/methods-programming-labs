"""Тесты vitals.bench.stats: медиана, сводка прогонов, коэффициент сокращения, скорость."""

from __future__ import annotations

import pytest
from vitals.bench.stats import (
    common_prefix_identical,
    median,
    rate,
    readings_comparable,
    reduction_ratio,
    speedup,
    summarize_runs,
)


def test_median_odd_and_even_and_empty():
    assert median([1, 3, 2]) == 2.0
    assert median([1, 2, 3, 4]) == 2.5
    assert median([7]) == 7.0
    with pytest.raises(ValueError):
        median([])


def test_summarize_runs_groups_in_first_appearance_order_and_takes_median():
    runs = [
        {"tool": "go", "devices": 100, "rate": 10.0},
        {"tool": "py", "devices": 100, "rate": 5.0},
        {"tool": "go", "devices": 100, "rate": 30.0},
        {"tool": "go", "devices": 100, "rate": 20.0},
        {"tool": "go", "devices": 200, "rate": 1.0},
    ]
    rows = summarize_runs(runs, group_keys=["tool", "devices"], metrics=["rate"])
    assert [(r["tool"], r["devices"]) for r in rows] == [("go", 100), ("py", 100), ("go", 200)]
    go100 = rows[0]
    assert (go100["runs"], go100["rate"]) == (3, 20.0)
    assert rows[1]["runs"] == 1 and rows[1]["rate"] == 5.0


def test_summarize_runs_empty_input_gives_no_rows():
    assert summarize_runs([], group_keys=["tool"], metrics=["rate"]) == []


def test_rate_is_per_second_and_zero_for_zero_time():
    assert rate(10, 2) == 5.0
    assert rate(10, 0) == 0.0


def test_reduction_ratio_raw_over_agg():
    assert reduction_ratio(1000, 10) == 100.0
    assert reduction_ratio(5, 0) == 0.0


def test_speedup_slow_over_fast():
    assert speedup(10, 2) == 5.0
    assert speedup(1, 0) == 0.0


def test_readings_comparable_requires_both_collectors_to_collect():
    assert readings_comparable(1, 1) is True
    assert readings_comparable(0, 5) is False
    assert readings_comparable(5, 0) is False


def test_common_prefix_identical_compares_overlap_only():
    assert common_prefix_identical(["a", "b"], ["a", "b", "c"]) is True
    assert common_prefix_identical(["a", "x"], ["a", "b"]) is False
    assert common_prefix_identical([], []) is False

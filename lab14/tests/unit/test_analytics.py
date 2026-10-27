"""Тесты vitals.analytics: шаги очистки, агрегаты, эпизоды (раздел 7), паритет DuckDB и Polars."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import polars as pl
from vitals.analytics import aggregate, clean, pipeline, queries
from vitals.analytics.schema import RAW_JSON_SCHEMA, TS_DTYPE

BASE = datetime(2026, 10, 13, 9, 30, tzinfo=UTC)


def iso(seconds: float) -> str:
    return (BASE + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(seconds % 1 * 1000):03d}Z"


def row(
    seq: int,
    ts: str | None = None,
    value: float | None = 72.0,
    metric: str = "heart_rate",
    unit: str = "bpm",
    battery: int | None = 80,
    device: str = "dev-0001",
    patient: str = "p-0001",
) -> dict[str, Any]:
    return {
        "device_id": device,
        "patient_id": patient,
        "seq": seq,
        "ts": ts if ts is not None else iso(seq),
        "metric": metric,
        "value": value,
        "unit": unit,
        "battery": battery,
    }


def frame(rows: list[dict[str, Any]]) -> pl.DataFrame:
    return pl.DataFrame(rows, schema=RAW_JSON_SCHEMA)


def typed(rows: list[dict[str, Any]]) -> pl.DataFrame:
    return clean.coerce_types(frame(rows))[0]


# --- шаги очистки -----------------------------------------------------------------------------------------------


def test_coerce_types_drops_unparsable_ts_and_reports_count():
    df = frame([row(1), row(2, ts="не дата"), row(3)])
    out, entry = clean.coerce_types(df)
    assert out.height == 2
    assert out.schema["ts"] == TS_DTYPE
    assert entry["step"] == "coerce_types"
    assert (entry["rows_before"], entry["rows_after"], entry["affected"]) == (3, 2, 1)


def test_drop_duplicates_by_device_and_seq_keeps_first():
    df = typed([row(1, value=70.0), row(1, value=99.0), row(1, value=99.0, device="dev-0002")])
    out, entry = clean.drop_duplicates(df)
    assert out.height == 2
    assert out.filter(pl.col("device_id") == "dev-0001")["value"].to_list() == [70.0]
    assert entry["affected"] == 1


def test_normalize_unit_fixes_case_and_empty_but_keeps_foreign_unit():
    df = typed([row(1, unit="BPM"), row(2, unit=""), row(3, unit="kg")])
    out, entry = clean.normalize_unit(df)
    assert out["unit"].to_list() == ["bpm", "bpm", "kg"]
    assert entry["affected"] == 2
    assert entry["rows_before"] == entry["rows_after"] == 3


def test_drop_future_removes_clock_glitch_in_middle_of_device():
    df = typed([
        row(1, ts=iso(0)),
        row(2, ts=iso(1)),
        row(3, ts=iso(3600 + 2)),  # сбой часов: +1 ч
        row(4, ts=iso(3)),
        row(5, ts=iso(4)),
    ])
    out, entry = clean.drop_future(df)
    assert out["seq"].to_list() == [1, 2, 4, 5]
    assert entry["affected"] == 1


def test_drop_future_removes_future_last_reading():
    df = typed([row(1, ts=iso(0)), row(2, ts=iso(1)), row(3, ts=iso(2)), row(4, ts=iso(3600))])
    out, entry = clean.drop_future(df)
    assert out["seq"].to_list() == [1, 2, 3]
    assert entry["affected"] == 1


def test_drop_future_keeps_small_skew_within_tolerance():
    df = typed([row(1, ts=iso(0)), row(2, ts=iso(4)), row(3, ts=iso(5))])
    out, entry = clean.drop_future(df)
    assert out.height == 3
    assert entry["affected"] == 0


def test_drop_invalid_removes_range_metric_and_unit_defects_with_breakdown():
    df = typed([
        row(1, value=72.0),
        row(2, value=999.0),  # вне диапазона
        row(3, metric="glucose", value=5.0, unit="mmol"),  # неизвестная метрика
        row(4, metric="spo2", value=95.0, unit="kg"),  # чужая единица
        row(5, value=None),  # пропуск: этот шаг его не трогает
    ])
    out, entry = clean.drop_invalid(df)
    assert out["seq"].to_list() == [1, 5]
    assert entry["affected"] == 3
    assert entry["detail"] == {"out_of_range": 1, "unknown_metric": 1, "bad_unit": 1}


def test_validate_boundaries_are_inclusive():
    df = typed([
        row(1, value=25.0),
        row(2, value=250.0),
        row(3, value=24.9),
        row(4, value=250.1),
        row(5, metric="temperature", value=38.0, unit="C"),
    ])
    out, _ = clean.drop_invalid(df)
    assert out["seq"].to_list() == [1, 2, 5]


def test_drop_missing_removes_null_value():
    df = typed([row(1), row(2, value=None), row(3)])
    out, entry = clean.drop_missing(df)
    assert out["seq"].to_list() == [1, 3]
    assert entry["affected"] == 1


def test_fill_battery_forward_fills_per_device_and_keeps_leading_gap():
    df = typed([row(1, battery=None), row(2, battery=80), row(3, battery=None)])
    out, entry = clean.fill_battery(df)
    assert out.sort("seq")["battery"].to_list() == [None, 80, 80]
    assert entry["affected"] == 1


def test_full_pipeline_report_counts_each_step():
    rows = [
        row(1, battery=80),
        row(1, battery=80),  # дубликат
        row(2, value=999.0),  # диапазон
        row(3, unit="BPM", battery=None),  # единица исправляется, батарея заполняется
        row(4, value=None),  # пропуск
        row(5, ts="битый"),  # ts не разбирается
        row(6, metric="spo2", unit="kg", value=95.0),  # чужая единица
    ]
    cleaned, report = clean.clean(frame(rows))
    by_step = {r["step"]: r for r in report}
    assert [r["step"] for r in report] == [
        "coerce_types",
        "drop_duplicates",
        "normalize_unit",
        "drop_future",
        "validate",
        "drop_missing",
        "fill_battery",
    ]
    assert by_step["coerce_types"]["affected"] == 1
    assert by_step["drop_duplicates"]["affected"] == 1
    assert by_step["normalize_unit"]["affected"] == 1
    assert by_step["normalize_unit"]["rows_before"] == by_step["normalize_unit"]["rows_after"]
    assert by_step["drop_future"]["affected"] == 0
    assert by_step["validate"]["affected"] == 2
    assert by_step["drop_missing"]["affected"] == 1
    assert by_step["fill_battery"]["affected"] == 1
    assert cleaned["seq"].to_list() == [1, 3]
    assert cleaned.filter(pl.col("seq") == 3)["unit"].to_list() == ["bpm"]
    assert cleaned.filter(pl.col("seq") == 3)["battery"].to_list() == [80]


# --- агрегаты ---------------------------------------------------------------------------------------------------


def test_aggregate_stats_match_manual_calculation():
    df = typed([
        row(1, value=60.0, patient="p-1"),
        row(2, value=80.0, patient="p-1"),
        row(3, value=100.0, patient="p-1"),
        row(4, value=95.0, patient="p-2", metric="spo2", unit="%"),
        row(5, value=97.0, patient="p-2", metric="spo2", unit="%"),
    ])
    stats = {(r["patient_id"], r["metric"]): r for r in aggregate.aggregate_stats(df).to_dicts()}
    hr = stats[("p-1", "heart_rate")]
    assert (hr["sum"], hr["avg"], hr["min"], hr["max"], hr["count"]) == (240.0, 80.0, 60.0, 100.0, 3)
    sp = stats[("p-2", "spo2")]
    assert (sp["sum"], sp["avg"], sp["min"], sp["max"], sp["count"]) == (192.0, 96.0, 95.0, 97.0, 2)


def test_hourly_profile_splits_by_hour():
    df = typed([row(1, ts="2026-10-13T09:59:50.000Z", value=60.0), row(2, ts="2026-10-13T10:00:10.000Z", value=90.0)])
    hourly = aggregate.hourly_profile(df).to_dicts()
    assert [(h["count"], h["avg"]) for h in hourly] == [(1, 60.0), (1, 90.0)]


# --- эпизоды (раздел 7) -----------------------------------------------------------------------------------------


def _episodes(rows: list[dict[str, Any]], alert: str) -> list[dict[str, Any]]:
    minutes = aggregate.minute_stats(typed(rows))
    eps = aggregate.find_episodes(minutes)
    return eps.filter(pl.col("alert") == alert).sort("start").to_dicts()


def test_tachycardia_episodes_split_on_gap_and_boundary_is_strict():
    rows = [
        row(1, ts=iso(0), value=125.0), row(2, ts=iso(30), value=125.0),
        row(3, ts=iso(60), value=130.0), row(4, ts=iso(90), value=130.0),
        row(5, ts=iso(120), value=100.0), row(6, ts=iso(150), value=100.0),
        row(7, ts=iso(180), value=121.0), row(8, ts=iso(210), value=121.0),
    ]
    eps = _episodes(rows, "tachycardia")
    assert len(eps) == 2
    assert eps[0]["start"] == BASE and eps[0]["end"] == BASE + timedelta(minutes=2)
    assert (eps[0]["minutes"], eps[0]["extreme"]) == (2, 130.0)
    assert eps[1]["start"] == BASE + timedelta(minutes=3)
    assert (eps[1]["minutes"], eps[1]["extreme"]) == (1, 121.0)


def test_tachycardia_mean_exactly_120_is_not_an_episode():
    rows = [row(1, ts=iso(0), value=120.0), row(2, ts=iso(30), value=120.0)]
    assert _episodes(rows, "tachycardia") == []


def test_desaturation_uses_minute_minimum_below_92():
    rows = [
        row(1, ts=iso(0), value=90.0, metric="spo2", unit="%"),
        row(2, ts=iso(30), value=95.0, metric="spo2", unit="%"),
        row(3, ts=iso(60), value=92.0, metric="spo2", unit="%"),
    ]
    eps = _episodes(rows, "desaturation")
    assert len(eps) == 1
    assert eps[0]["extreme"] == 90.0 and eps[0]["minutes"] == 1


def test_fever_uses_last_value_strictly_above_38():
    rows = [
        row(1, ts=iso(0), value=37.5, metric="temperature", unit="C"),
        row(2, ts=iso(30), value=38.2, metric="temperature", unit="C"),
        row(3, ts=iso(60), value=38.0, metric="temperature", unit="C"),
    ]
    eps = _episodes(rows, "fever")
    assert len(eps) == 1
    assert eps[0]["extreme"] == 38.2


def test_hypertension_uses_five_minute_rolling_mean_of_systolic():
    high = [row(i, ts=iso(i * 60 + 1), value=170.0, metric="systolic_bp", unit="mmHg") for i in range(3)]
    low = [row(i + 10, ts=iso(i * 60 + 1), value=150.0, metric="systolic_bp", unit="mmHg") for i in range(3)]
    assert len(_episodes(high, "hypertension")) == 1
    assert _episodes(low, "hypertension") == []


def test_sensor_fault_is_not_computed_on_clean_data():
    rows = [row(1, ts=iso(0))]
    assert _episodes(rows, "sensor_fault") == []


# --- паритет DuckDB и Polars на одном Parquet --------------------------------------------------------------------


def test_duckdb_and_polars_return_same_results_on_one_parquet(tmp_path: Path):
    rows = []
    for i in range(1, 301):
        if i % 2:
            rows.append(row(i, ts=iso(i), value=60.0 + i % 40, patient=f"p-{i % 3}", device=f"dev-{i % 2}"))
        else:
            rows.append(row(i, ts=iso(i), value=95.0 + i % 5, metric="spo2", unit="%", patient=f"p-{i % 3}"))
    cleaned, _ = clean.clean(frame(rows))
    clean_dir = tmp_path / "clean"
    pipeline.write_clean_parquet(cleaned, clean_dir)

    con = duckdb.connect()
    try:
        for name in queries.SQL:
            duck = queries.run_duckdb(con, name, clean_dir)
            poly = queries.run_polars(name, clean_dir)
            assert queries.frames_match(duck, poly), name
    finally:
        con.close()
    assert queries.benchmark(clean_dir, repeats=1)["all_match"] is True

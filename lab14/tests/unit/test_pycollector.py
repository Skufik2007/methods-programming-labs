"""Тесты vitals.pycollector: tumbling-окна (раздел 2.4), формат JSONL raw и agg, запись на диск, чекпоинты."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from vitals.pycollector.model import (
    Agg,
    Reading,
    agg_line,
    fmt_float,
    format_ts,
    parse_ts_ms,
    reading_from_json,
    reading_line,
    valid_value,
)
from vitals.pycollector.sink import FileCheckpoints, FilesSink
from vitals.pycollector.windows import AddResult, TumblingAggregator

SIZE = 10_000  # WINDOW = 10 с
LATENESS = 2_000  # WINDOW_LATENESS = 2 с


def rd(seq: int, ts_ms: int, value: float | None = 72.0, metric: str = "heart_rate",
       device: str = "dev-0001", patient: str = "p-0001", battery: int | None = 80) -> Reading:
    return Reading(device, patient, seq, ts_ms, metric, value, "bpm", battery, format_ts(ts_ms))


def new_agg() -> TumblingAggregator:
    return TumblingAggregator("collector-1", SIZE, LATENESS)


# --- tumbling-окна ----------------------------------------------------------------------------------------------


def test_windows_are_half_open_and_aligned_to_epoch():
    a = new_agg()
    a.add(rd(1, 9_999), now_ms=9_999)
    a.add(rd(2, 10_000), now_ms=10_000)
    assert a.close_due(11_999) == []  # окно [0,10000) ещё ждёт опоздавших
    closed = a.close_due(12_001)
    assert [(x.window_start_ms, x.window_end_ms, x.count) for x in closed] == [(0, 10_000, 1)]


def test_window_closes_at_end_plus_lateness():
    # Генератор тестов ждал строгого «>» по фразе «ушло за window_end + lateness». Go- и
    # Python-сборщик одинаково закрывают окно при now >= window_end + lateness; формулировка
    # контракта уточнена (раздел 2.4), находка отклонена.
    a = new_agg()
    a.add(rd(1, 1_000), now_ms=1_000)
    assert a.close_due(SIZE + LATENESS - 1) == []
    assert len(a.close_due(SIZE + LATENESS)) == 1


def test_aggregate_values_for_one_window():
    a = new_agg()
    for seq, (ts, v) in enumerate([(1_000, 60.0), (2_000, 80.0), (3_000, 100.0)], start=1):
        assert a.add(rd(seq, ts, value=v), now_ms=3_000) is AddResult.ACCEPTED
    (out,) = a.close_all(now_ms=3_000)
    assert (out.count, out.invalid, out.sum, out.min, out.max, out.last) == (3, 0, 240.0, 60.0, 100.0, 100.0)
    assert (out.window_start_ms, out.window_end_ms, out.collector_id) == (0, SIZE, "collector-1")


def test_invalid_and_null_values_count_but_do_not_enter_sum():
    a = new_agg()
    a.add(rd(1, 1_000, value=72.0), now_ms=1_000)
    a.add(rd(2, 2_000, value=999.0), now_ms=2_000)  # вне диапазона
    a.add(rd(3, 3_000, value=None), now_ms=3_000)  # сбой датчика
    (out,) = a.close_all(now_ms=3_000)
    # count, как в mean = sum/count (контракт 2.4), - только валидные; невалидные - в invalid
    assert (out.count, out.invalid, out.sum, out.min, out.max) == (1, 2, 72.0, 72.0, 72.0)


def test_duplicate_seq_is_skipped_and_not_aggregated():
    a = new_agg()
    assert a.add(rd(1, 1_000), now_ms=1_000) is AddResult.ACCEPTED
    assert a.add(rd(1, 1_000), now_ms=1_000) is AddResult.DUPLICATE
    assert a.stats.dup_skipped == 1
    (out,) = a.close_all(now_ms=1_000)
    assert out.count == 1


def test_seq_not_above_last_seen_is_a_repeat():
    # Генератор тестов считал, что меньший, ещё не виденный seq — не повтор. Эмулятор отдаёт
    # показания устройства по возрастанию seq, а одно устройство опрашивается последовательно,
    # поэтому seq ≤ последнего виденного может быть только повтором (как и в Go-агрегаторе).
    # Предусловие записано в контракт (раздел 4), находка отклонена.
    a = new_agg()
    a.add(rd(3, 3_000), now_ms=3_000)
    assert a.add(rd(3, 3_000), now_ms=3_000) is AddResult.DUPLICATE
    assert a.add(rd(2, 2_000), now_ms=3_000) is AddResult.DUPLICATE


def test_late_reading_after_window_closed_is_counted():
    a = new_agg()
    assert a.add(rd(1, 1_000), now_ms=20_000) is AddResult.LATE
    assert a.stats.late_dropped == 1
    assert a.close_all(now_ms=20_000) == []


def test_future_timestamp_is_clamped_to_now_and_marked_invalid():
    a = new_agg()
    now = 100_000
    a.add(rd(1, now + 120_000, value=72.0), now_ms=now)  # +2 мин: сбой часов устройства
    assert a.stats.future_clamped == 1
    (out,) = a.close_all(now_ms=now)
    assert (out.window_start_ms, out.count, out.invalid) == (100_000, 0, 1)


def test_small_clock_skew_within_one_minute_is_not_clamped():
    a = new_agg()
    now = 100_000
    a.add(rd(1, now + 30_000, value=72.0), now_ms=now)
    assert a.stats.future_clamped == 0
    (out,) = a.close_all(now_ms=now)
    assert out.window_start_ms == 130_000 and out.invalid == 0


def test_close_all_marks_unfinished_windows_as_early_closed():
    a = new_agg()
    a.add(rd(1, 1_000), now_ms=2_000)
    out = a.close_all(now_ms=3_000)
    assert len(out) == 1
    assert a.stats.early_closed == 1 and a.stats.closed == 1 and a.stats.open == 0


def test_closed_windows_are_sorted_by_start_device_metric():
    a = new_agg()
    a.add(rd(1, 15_000, device="dev-0002"), now_ms=15_000)
    a.add(rd(2, 5_000, device="dev-0001"), now_ms=5_000)
    a.add(rd(3, 5_000, device="dev-0001", metric="spo2", value=97.0), now_ms=5_000)
    keys = [(x.window_start_ms, x.device_id, x.metric) for x in a.close_all(now_ms=20_000)]
    assert keys == sorted(keys)


# --- формат JSONL по контракту ----------------------------------------------------------------------------------


def test_raw_line_matches_contract_bytes():
    r = rd(123, parse_ts_ms("2026-10-13T09:30:00.250Z"), value=72.0, battery=87)
    line = reading_line(r)
    assert line == (
        '{"device_id":"dev-0001","patient_id":"p-0001","seq":123,"ts":"2026-10-13T09:30:00.250Z",'
        '"metric":"heart_rate","value":72.0,"unit":"bpm","battery":87}\n'
    )


def test_raw_line_nulls_for_missing_value_and_battery():
    line = reading_line(rd(1, 0, value=None, battery=None))
    assert '"value":null' in line and '"battery":null' in line


def test_raw_line_keeps_cyrillic_as_is():
    line = reading_line(rd(1, 0, patient="п-0001"))
    assert '"patient_id":"п-0001"' in line


def test_agg_line_matches_contract_example():
    a = Agg(0, 10_000, "dev-0001", "p-0001", "heart_rate", 10, 1, 731.0, 68.0, 79.0, 75.0, "collector-1")
    assert agg_line(a) == (
        '{"window_start":"1970-01-01T00:00:00Z","window_end":"1970-01-01T00:00:10Z",'
        '"device_id":"dev-0001","patient_id":"p-0001","metric":"heart_rate",'
        '"count":10,"invalid":1,"sum":731.0,"min":68.0,"max":79.0,"mean":73.1,"last":75.0,'
        '"collector_id":"collector-1"}\n'
    )


def test_agg_line_for_empty_window_uses_zero_sum_and_nulls():
    a = Agg(0, 10_000, "dev-0001", "p-0001", "heart_rate", 0, 0, 0.0, 0.0, 0.0, 0.0, "c1")
    obj = json.loads(agg_line(a))
    assert obj["sum"] == 0.0
    assert obj["min"] is None and obj["max"] is None and obj["mean"] is None and obj["last"] is None


def test_agg_line_is_valid_json_with_all_contract_keys():
    a = Agg(60_000, 70_000, "dev-0002", "p-0002", "spo2", 3, 0, 291.0, 96.0, 98.0, 97.0, "c1")
    obj = json.loads(agg_line(a))
    assert set(obj) == {
        "window_start", "window_end", "device_id", "patient_id", "metric", "count", "invalid",
        "sum", "min", "max", "mean", "last", "collector_id",
    }
    assert obj["mean"] == 97.0


def test_fmt_float_never_uses_exponent_and_keeps_point():
    assert fmt_float(72) == "72.0"
    assert fmt_float(0.1) == "0.1"
    assert fmt_float(1e16) == "10000000000000000.0"
    assert "e" not in fmt_float(1e-7).lower()


def test_format_ts_truncates_to_milliseconds_and_round_trips():
    ms = parse_ts_ms("2026-10-13T09:30:00.250Z")
    assert format_ts(ms) == "2026-10-13T09:30:00.250Z"
    assert ms == int(datetime(2026, 10, 13, 9, 30, 0, 250_000, tzinfo=UTC).timestamp() * 1000)


def test_reading_from_json_canonicalises_non_millisecond_ts():
    r = reading_from_json({"device_id": "d", "patient_id": "p", "seq": 1, "ts": "2026-10-13T09:30:00Z",
                           "metric": "heart_rate", "value": 70, "unit": "bpm", "battery": None})
    assert r.ts_text == "2026-10-13T09:30:00.000Z"
    assert r.value == 70.0 and r.battery is None


def test_valid_value_boundaries_and_non_finite():
    assert valid_value("heart_rate", 25.0) and valid_value("heart_rate", 250.0)
    assert not valid_value("heart_rate", 24.99) and not valid_value("heart_rate", 250.01)
    assert not valid_value("heart_rate", float("nan"))
    assert not valid_value("heart_rate", float("inf"))
    assert not valid_value("heart_rate", None)
    assert not valid_value("glucose", 5.0)


# --- запись и чекпоинты -----------------------------------------------------------------------------------------


def test_files_sink_appends_batch_in_one_write_and_counts(tmp_path: Path):
    clock = {"now": datetime(2026, 10, 13, 9, 59, tzinfo=UTC)}
    sink = FilesSink(tmp_path, "c1", now=lambda: clock["now"])
    lines = [reading_line(rd(1, 0)), reading_line(rd(2, 1_000))]
    sink.write_and_sync("raw", lines)
    sink.write_and_sync("raw", [])  # пустой пакет ничего не пишет
    sink.close()
    f = tmp_path / "raw" / "c1-2026101309.jsonl"
    assert f.read_text(encoding="utf-8") == "".join(lines)
    assert sink.records["raw"] == 2
    assert sink.bytes["raw"] == len("".join(lines).encode("utf-8"))


def test_files_sink_rolls_over_to_new_file_on_hour_change(tmp_path: Path):
    clock = {"now": datetime(2026, 10, 13, 9, 59, tzinfo=UTC)}
    sink = FilesSink(tmp_path, "c1", now=lambda: clock["now"])
    sink.write_and_sync("agg", [agg_line(Agg(0, 10_000, "d", "p", "m", 1, 0, 1.0, 1.0, 1.0, 1.0, "c1"))])
    clock["now"] = datetime(2026, 10, 13, 10, 0, 1, tzinfo=UTC)
    sink.write_and_sync("agg", [agg_line(Agg(0, 10_000, "d", "p", "m", 1, 0, 2.0, 2.0, 2.0, 2.0, "c1"))])
    sink.close()
    assert (tmp_path / "agg" / "c1-2026101309.jsonl").exists()
    assert (tmp_path / "agg" / "c1-2026101310.jsonl").exists()


def test_checkpoints_only_grow_and_survive_reload(tmp_path: Path):
    path = tmp_path / "checkpoints.json"
    cp = FileCheckpoints(path)
    assert cp.load("dev-0001") == 0
    cp.save({"dev-0001": 5})
    cp.save({"dev-0001": 3})  # старый владелец не откатывает чекпоинт
    assert cp.load("dev-0001") == 5
    again = FileCheckpoints(path)
    assert again.load("dev-0001") == 5
    assert json.loads(path.read_text(encoding="utf-8")) == {"dev-0001": 5}

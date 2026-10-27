"""Сценарий raw_vs_agg: объём и число записей на выходе сборщика, raw против agg (WINDOW 10 с и 60 с).

Эмулятор работает в реальном времени (START = сейчас): иначе все окна «старые» и закрываются как
опоздавшие. При остановке открытые окна закрываются досрочно, поэтому часть agg-строк — неполные;
для устойчивого коэффициента берём медиану readings-на-agg-строку (count + invalid).
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any

import polars as pl

from vitals.bench import procs
from vitals.bench.plots import grouped_bars, panels, save_figure
from vitals.bench.results import IMG_DIR, write_result
from vitals.bench.stats import median, reduction_ratio


def one_case(kind: str, devices: int, window: int, duration: float) -> dict[str, Any]:
    out = Path(tempfile.mkdtemp(prefix=f"bench-rva-{kind}-"))
    try:
        with procs.simulator(procs.free_port(18090), devices, 0) as url:
            env = procs.collector_env(url, out, procs.free_port(19110), emit="both", window=window)
            m = procs.run_measured(procs.collector_command(kind, duration), env, timeout=duration + 60)
        raw_dir, agg_dir = out / "raw", out / "agg"
        raw_n = procs.count_lines(raw_dir) if raw_dir.exists() else 0
        agg_n = procs.count_lines(agg_dir) if agg_dir.exists() else 0
        raw_b = procs.dir_bytes(raw_dir) if raw_dir.exists() else 0
        agg_b = procs.dir_bytes(agg_dir) if agg_dir.exists() else 0
        per_row = None
        if agg_n:
            files = sorted(agg_dir.glob("*.jsonl"))
            df = pl.concat([pl.read_ndjson(f, schema_overrides={"min": pl.Float64}) for f in files])
            per_row = median((df["count"] + df["invalid"]).to_list())
        return {
            "collector": kind, "devices": devices, "window_s": window, "duration_s": duration,
            "exit_code": m.exit_code, "raw_records": raw_n, "agg_records": agg_n,
            "raw_bytes": raw_b, "agg_bytes": agg_b,
            "records_ratio": reduction_ratio(raw_n, agg_n), "bytes_ratio": reduction_ratio(raw_b, agg_b),
            "median_readings_per_agg_row": per_row,
        }
    finally:
        shutil.rmtree(out, ignore_errors=True)


def run(quick: bool = False, devices: int | None = None, windows: list[int] | None = None,
        collector: str = "go") -> dict[str, Any]:
    procs.ensure_go_binaries()
    devices = devices or (20 if quick else 50)
    windows = windows or [10, 60]
    cases = []
    for w in windows:
        duration = w + (15 if quick else 25)  # минимум одно полное окно + хвост на закрытие
        case = one_case(collector, devices, w, duration)
        cases.append(case)
        print(f"  WINDOW={w}s: raw {case['raw_records']} зап./{case['raw_bytes']} Б, agg {case['agg_records']} зап./"
              f"{case['agg_bytes']} Б, сокращение {case['records_ratio']:.1f}x по записям, "
              f"{case['bytes_ratio']:.1f}x по байтам", flush=True)
    path = write_result("raw_vs_agg", {"params": {"quick": quick, "devices": devices, "windows": windows,
                                                  "collector": collector}, "cases": cases})
    plot(cases)
    print(f"-> {path}")
    return {"cases": cases}


def plot(cases: list[dict[str, Any]]) -> Path:
    labels = [f"WINDOW {c['window_s']} с" for c in cases]
    fig, axes = panels(2)
    grouped_bars(axes[0], labels, {"raw": [c["raw_records"] for c in cases], "agg": [c["agg_records"] for c in cases]},
                 "записей", "Число записей", log=True)
    grouped_bars(axes[1], labels, {"raw": [c["raw_bytes"] / 1e6 for c in cases],
                                   "agg": [c["agg_bytes"] / 1e6 for c in cases]}, "МБ", "Объём на диске", log=True)
    return save_figure(fig, "bench_raw_vs_agg.png", IMG_DIR)

"""Сборка шагов в конвейер: загрузка -> очистка -> агрегаты -> Parquet -> DuckDB/Polars -> графики."""

from __future__ import annotations

import json
import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import polars as pl

from vitals.analytics import aggregate, clean, load, plots, queries
from vitals.common import get_logger

log = get_logger("vitals.analytics")


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def write_clean_parquet(df: pl.DataFrame, clean_dir: Path) -> list[str]:
    """Parquet с Hive-партиционированием по metric: clean/metric=<m>/part-0.parquet (metric — в имени каталога)."""
    if clean_dir.exists():
        shutil.rmtree(clean_dir)  # прежний прогон не должен подмешиваться
    written = []
    for (metric,), part in df.partition_by("metric", as_dict=True, maintain_order=True).items():
        folder = clean_dir / f"metric={metric}"
        folder.mkdir(parents=True)
        part.drop("metric").write_parquet(folder / "part-0.parquet", compression="zstd")
        written.append(folder.name)
    return written


def load_raw(source: str, in_dir: Path | None, flight: Sequence[str]) -> tuple[pl.DataFrame, pl.DataFrame | None]:
    """Вернуть (raw, agg|None) как DataFrame из выбранного источника."""
    if source == "flight":
        if not flight:
            raise SystemExit("для --source flight нужен хотя бы один --flight grpc://host:port")
        return load.fetch_flight(flight, "raw"), load.fetch_flight(flight, "agg")
    if in_dir is None:
        raise SystemExit("для --source files нужен --in <каталог с raw/ и agg/>")
    raw_lf = load.scan_jsonl(in_dir, "raw")
    if raw_lf is None:
        raise SystemExit(f"в {in_dir / 'raw'} нет *.jsonl")
    agg_lf = load.scan_jsonl(in_dir, "agg")
    return raw_lf.collect(), (agg_lf.collect() if agg_lf is not None else None)


def run(
    source: str,
    in_dir: Path | None,
    out_dir: Path,
    flight: Sequence[str] = (),
    repeats: int = 5,
    with_plots: bool = True,
) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    raw, agg_raw = load_raw(source, in_dir, flight)
    summary = {"raw": load.summarize(raw)}
    print(load.format_summary("raw", summary["raw"]))
    if agg_raw is not None:
        summary["agg"] = load.summarize(agg_raw)
        print(load.format_summary("agg", summary["agg"]))
    write_json(out_dir / "summary.json", summary)
    log.info("данные загружены", extra={"source": source, "raw_rows": raw.height})

    cleaned, report = clean.clean(raw)
    write_json(out_dir / "cleaning_report.json", report)
    for entry in report:
        log.info("шаг очистки", extra=entry)

    stats = aggregate.aggregate_stats(cleaned)
    hourly = aggregate.hourly_profile(cleaned)
    minutes = aggregate.minute_stats(cleaned)
    episodes = aggregate.find_episodes(minutes)
    stats.write_parquet(out_dir / "agg.parquet")
    hourly.write_parquet(out_dir / "hourly.parquet")
    episodes.write_parquet(out_dir / "episodes.parquet")
    if agg_raw is not None:
        aggregate.typed_windows(agg_raw).write_parquet(out_dir / "agg_windows.parquet")
    partitions = write_clean_parquet(cleaned, out_dir / "clean")

    timings = queries.benchmark(out_dir / "clean", repeats=repeats)
    write_json(out_dir / "timings.json", timings)
    if not timings["all_match"]:
        log.error("результаты DuckDB и Polars не совпали", extra={"queries": timings["queries"]})
        raise RuntimeError("результаты DuckDB и Polars не совпали, см. timings.json")

    images = plots.make_plots(cleaned, episodes, report, out_dir / "img") if with_plots else []
    result = {
        "raw_rows": raw.height,
        "clean_rows": cleaned.height,
        "partitions": partitions,
        "episodes": episodes.height,
        "images": images,
    }
    write_json(out_dir / "result.json", result)
    return result

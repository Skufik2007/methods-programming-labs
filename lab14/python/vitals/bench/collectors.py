"""Сценарий collectors: Go- и Python-сборщик против одного эмулятора при одинаковой нагрузке.

Нагрузка — вычитывание накопленного: START эмулятора в прошлом, у каждого устройства есть
десятки тысяч показаний, и скорость упирается в сборщик, а не в темп эмулятора (7 показаний/с на
устройство). Режим EMIT=raw: у «старых» показаний окна уже закрыты (late_dropped), агрегация
здесь ничего не измерила бы.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any

from vitals.bench import procs
from vitals.bench.plots import grouped_bars, panels, save_figure
from vitals.bench.results import IMG_DIR, write_result
from vitals.bench.stats import common_prefix_identical, median, rate, readings_comparable, summarize_runs

SAMPLE_DEVICES = 3  # у скольких устройств сверяем строки сборщиков побайтно
# Находка №5: при BATCH_SIZE=500 оба сборщика упирались в fsync после каждого пакета
# (~150 fsync/с), и замер сравнивал скорость синхронизации диска, а не языки:
# Go давал 72 тыс. показаний/с при 500, 334 тыс. при 5000 и 446 тыс. при 20000.
# Пропускную способность меряем с большим пакетом, влияние размера пакета — отдельно (batch_sweep).
BENCH_BATCH = 10_000
SWEEP_BATCHES = (500, 5_000, 20_000)
SAMPLE_LINES = 2000


def sample_lines(raw_dir: Path, wanted: set[str]) -> dict[str, list[str]]:
    """Первые SAMPLE_LINES строк указанных устройств из raw/*.jsonl (порядок по устройству сохранён)."""
    out: dict[str, list[str]] = {d: [] for d in wanted}
    prefix = len('{"device_id":"')
    for f in sorted(raw_dir.glob("*.jsonl")):
        with open(f, encoding="utf-8", newline="") as fh:
            for line in fh:
                dev = line[prefix:line.index('"', prefix)]
                lst = out.get(dev)
                if lst is not None and len(lst) < SAMPLE_LINES:
                    lst.append(line)
    return out


def one_run(kind: str, sim_url: str, devices: int, duration: float, workers: int, port: int,
            batch: int = BENCH_BATCH) -> dict[str, Any]:
    out = Path(tempfile.mkdtemp(prefix=f"bench-{kind}-"))  # свежий OUT_DIR на каждый прогон
    try:
        env = procs.collector_env(sim_url, out, port, workers=workers, emit="raw",
                                  extra={"BATCH_SIZE": str(batch)})
        m = procs.run_measured(procs.collector_command(kind, duration), env, timeout=duration + 60)
        raw_dir = out / "raw"
        readings = procs.count_lines(raw_dir) if raw_dir.exists() else 0
        wanted = {f"dev-{i:04d}" for i in range(1, min(devices, SAMPLE_DEVICES) + 1)}
        samples = sample_lines(raw_dir, wanted) if readings else {}
        return {
            "collector": kind, "batch_size": batch, "exit_code": m.exit_code, "readings": readings,
            "readings_per_s": rate(readings, duration), "bytes": procs.dir_bytes(raw_dir) if readings else 0,
            "peak_rss_mb": m.peak_rss_mb, "cpu_s": m.cpu_s, "cpu_pct": m.cpu_pct, "wall_s": m.wall_s,
            "_samples": samples,
        }
    finally:
        shutil.rmtree(out, ignore_errors=True)


def run(quick: bool = False, devices: list[int] | None = None, latencies: list[int] | None = None,
        repeats: int | None = None, duration: float | None = None, workers: int = 16) -> dict[str, Any]:
    procs.ensure_go_binaries()
    devices = devices or ([200] if quick else [50, 200, 500])
    latencies = latencies if latencies is not None else [0, 20]
    repeats = repeats or (1 if quick else 3)
    duration = duration or (10.0 if quick else 20.0)
    runs: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []
    sim_port = procs.free_port(18090)
    status_port = procs.free_port(19110)
    for n in devices:
        for lat in latencies:
            # 6 часов накопленных показаний = 21600 на устройство: за 10–20 с не исчерпать
            with procs.simulator(sim_port, n, lat, backlog_hours=6) as url:
                for rep in range(repeats):
                    got: dict[str, dict[str, Any]] = {}
                    for kind in ("go", "python"):  # чередуем порядок не нужно: эмулятор без состояния
                        r = one_run(kind, url, n, duration, workers, status_port)
                        r.update(devices=n, latency_ms=lat, repeat=rep)
                        got[kind] = r
                        print(f"  devices={n} lat={lat}ms #{rep + 1} {kind}: {r['readings_per_s']:.0f} показ./с, "
                              f"RSS {r['peak_rss_mb']:.0f} МБ, CPU {r['cpu_pct']:.0f}%", flush=True)
                    s_go, s_py = got["go"].pop("_samples"), got["python"].pop("_samples")
                    same = [common_prefix_identical(s_go.get(d, []), s_py.get(d, [])) for d in s_go]
                    checks.append({
                        "devices": n, "latency_ms": lat, "repeat": rep,
                        "comparable": readings_comparable(got["go"]["readings"], got["python"]["readings"]),
                        "prefix_identical": bool(same) and all(same),
                        "ratio_go_to_python": (got["go"]["readings"] / got["python"]["readings"]
                                               if got["python"]["readings"] else None),
                    })
                    runs.extend(got.values())
    summary = summarize_runs(runs, ("devices", "latency_ms", "collector"),
                             ("readings_per_s", "peak_rss_mb", "cpu_pct", "cpu_s"))
    params = {"quick": quick, "devices": devices, "latency_ms": latencies, "repeats": repeats,
              "duration_s": duration, "workers": workers, "emit": "raw", "backlog_hours": 6,
              "batch_size": BENCH_BATCH}
    sweep = batch_sweep(sim_port, status_port, duration, workers, 1 if quick else repeats)
    path = write_result("collectors", {"params": params, "runs": runs, "summary": summary, "checks": checks,
                                       "batch_sweep": sweep})
    plot(summary)
    print(f"-> {path}")
    return {"summary": summary, "checks": checks}


def plot(summary: list[dict[str, Any]]) -> Path:
    cats = sorted({(s["devices"], s["latency_ms"]) for s in summary})
    labels = [f"{d} устр.\n{lat} мс" for d, lat in cats]
    fig, axes = panels(3, 5.5)
    specs = [("readings_per_s", "показаний/с", "Пропускная способность"),
             ("peak_rss_mb", "МБ", "Пиковая RSS"), ("cpu_pct", "% одного ядра", "CPU")]
    for ax, (key, ylabel, title) in zip(axes, specs, strict=True):
        series = {k: [median(s[key] for s in summary if (s["devices"], s["latency_ms"]) == c and s["collector"] == k)
                      for c in cats] for k in ("go", "python")}
        grouped_bars(ax, labels, series, ylabel, title)
    return save_figure(fig, "bench_collectors.png", IMG_DIR)


def batch_sweep(sim_port: int, status_port: int, duration: float, workers: int, repeats: int) -> list[dict[str, Any]]:
    """Влияние BATCH_SIZE (а значит, частоты fsync) на пропускную способность обоих сборщиков."""
    runs: list[dict[str, Any]] = []
    with procs.simulator(sim_port, 200, 0, backlog_hours=6) as url:
        for batch in SWEEP_BATCHES:
            for kind in ("go", "python"):
                for rep in range(repeats):
                    r = one_run(kind, url, 200, duration, workers, status_port, batch=batch)
                    r.pop("_samples", None)
                    r.update(devices=200, latency_ms=0, repeat=rep)
                    runs.append(r)
                    print(f"  batch={batch} {kind}: {r['readings_per_s']:.0f} показ./с", flush=True)
    return summarize_runs(runs, ("batch_size", "collector"), ("readings_per_s", "cpu_pct"))

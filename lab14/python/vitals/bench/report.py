"""Сценарий report: собрать reports/bench/*.json в reports/bench/summary.md (таблицы + ссылки на графики)."""

from __future__ import annotations

from typing import Any

from vitals.bench.results import BENCH_DIR, read_result


def table(headers: list[str], rows: list[list[Any]]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def section_collectors(d: dict[str, Any]) -> str:
    p = d["params"]
    rows = [[s["devices"], s["latency_ms"], s["collector"], f"{s['readings_per_s']:.0f}",
             f"{s['peak_rss_mb']:.1f}", f"{s['cpu_pct']:.0f}", s["runs"]] for s in d["summary"]]
    checks = d.get("checks", [])
    ok = sum(1 for c in checks if c["comparable"] and c["prefix_identical"])
    return (f"## Go и Python сборщик\n\nEMIT={p['emit']}, WORKERS={p['workers']}, {p['duration_s']:g} с на прогон, "
            f"прогонов на точку: {p['repeats']} (медиана), эмулятор с накопленными данными за "
            f"{p['backlog_hours']} ч. CPU — % одного ядра.\n\n"
            + table(["устройств", "LATENCY_MS", "сборщик", "показ./с", "пик RSS, МБ", "CPU, %", "прогонов"], rows)
            + f"\n\nПроверка сопоставимости: {ok} из {len(checks)} пар прогонов — оба собрали данные и "
              "общие префиксы строк устройств совпали побайтно.\n\n![collectors](../img/bench_collectors.png)\n"
            + section_batch_sweep(d))


def section_batch_sweep(d: dict[str, Any]) -> str:
    """Влияние BATCH_SIZE: при fsync после каждого пакета маленький пакет упирается в диск (находка №5)."""
    sweep = d.get("batch_sweep") or []
    if not sweep:
        return ""
    rows = [[s["batch_size"], s["collector"], f"{s['readings_per_s']:.0f}", f"{s['cpu_pct']:.0f}", s["runs"]]
            for s in sweep]
    return ("\n### Размер пакета (fsync после каждого пакета)\n\n200 устройств, LATENCY_MS=0. Основная таблица "
            f"снята с BATCH_SIZE={d['params'].get('batch_size', '?')}.\n\n"
            + table(["BATCH_SIZE", "сборщик", "показ./с", "CPU, %", "прогонов"], rows) + "\n")


def section_transport(d: dict[str, Any]) -> str:
    rows = [[s["kind"], s["format"], f"{s['rows']:.0f}", f"{s['bytes_per_row']:.1f}",
             f"{s['transfer_s'] * 1000:.1f}", f"{s['load_s'] * 1000:.1f}", f"{s['total_us_per_row']:.2f}"]
            for s in d["summary"]]
    return ("## JSONL и Arrow Flight\n\n"
            + table(["набор", "формат", "строк", "Б/строку", "передача, мс", "загрузка, мс", "мкс/строку"], rows)
            + "\n\n![transport](../img/bench_transport.png)\n")


def section_raw_vs_agg(d: dict[str, Any]) -> str:
    rows = [[c["window_s"], c["raw_records"], c["agg_records"], f"{c['records_ratio']:.1f}", c["raw_bytes"],
             c["agg_bytes"], f"{c['bytes_ratio']:.1f}", c["median_readings_per_agg_row"]] for c in d["cases"]]
    return ("## raw и agg\n\n"
            + table(["WINDOW, с", "raw записей", "agg записей", "сокращение (записи)", "raw байт", "agg байт",
                     "сокращение (байты)", "показаний на agg-строку"], rows)
            + "\n\nЧасть agg-строк — неполные окна на старте и при остановке, поэтому устойчивое сокращение "
              "ближе к последнему столбцу.\n\n![raw_vs_agg](../img/bench_raw_vs_agg.png)\n")


def section_validation(d: dict[str, Any]) -> str:
    rows = [["Polars", f"{d['polars_s'] * 1000:.1f}", "1.00"]]
    if "rust_s" in d:
        rows.append(["Rust", f"{d['rust_s'] * 1000:.1f}", f"{d['speedup']:.2f}"])
    return (f"## Rust и Polars (валидация, {d['rows']:,} строк)\n\n"
            + table(["реализация", "время, мс (медиана)", "ускорение относительно Polars"], rows)
            + f"\n\nРезультаты совпали: {d.get('results_equal', 'н/д')}.\n\n"
            + "![validation](../img/bench_validation.png)\n")


SECTIONS = [("collectors", section_collectors), ("transport", section_transport),
            ("raw_vs_agg", section_raw_vs_agg), ("validation", section_validation)]


def run() -> str:
    parts = ["# Результаты замеров\n"]
    env = None
    for name, fn in SECTIONS:
        d = read_result(name)
        if d is None:
            parts.append(f"## {name}\n\nнет данных (сценарий не запускался).\n")
            continue
        env = env or d.get("env")
        parts.append(fn(d))
    if env:
        parts.insert(1, f"Окружение: {env['platform']}, Python {env['python']}, CPU: {env['cpu_count']}.\n")
    path = BENCH_DIR / "summary.md"
    path.write_text("\n".join(parts), encoding="utf-8")
    print(f"-> {path}")
    return str(path)

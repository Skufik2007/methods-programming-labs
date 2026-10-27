"""Объективные метрики решений C1: размер, замечания ruff, сложность, скорость.

    python comparison/task6/metrics.py            # все решения из solutions/*/iter*/slots.py

Скорость меряется на нагрузке в 4 раза тяжелее, чем в скрытом тесте: 200 врачей,
по 400 занятых интервалов, горизонт 14 дней, limit 5000 (медиана из 3 запусков).
"""

import importlib.util
import json
import random
import statistics
import subprocess
import sys
import time as clock
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
RUFF = ROOT / ".venv" / "Scripts" / "ruff.exe"
MSK = ZoneInfo("Europe/Moscow")


def load(path: Path):
    spec = importlib.util.spec_from_file_location(f"impl_{abs(hash(path))}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def bench(module) -> float:
    rnd = random.Random(7)
    base = datetime.combine(date(2026, 10, 12), time(9), tzinfo=MSK)
    doctors = [
        module.Doctor(
            id=i,
            specialty="Терапевт",
            work_days=frozenset({1, 2, 3, 4, 5, 6}),
            work_start=time(8),
            work_end=time(20),
            slot_minutes=rnd.choice([10, 15, 20, 30]),
            breaks=((time(13), time(14)),),
        )
        for i in range(1, 201)
    ]
    busy = {}
    for d in doctors:
        items = []
        for _ in range(400):
            start = base + timedelta(minutes=5 * rnd.randrange(0, 14 * 24 * 12))
            items.append((start, start + timedelta(minutes=rnd.choice([10, 15, 30]))))
        busy[d.id] = items
    runs = []
    for _ in range(3):
        started = clock.perf_counter()
        module.find_earliest_options(
            doctors, busy, [], specialty="терапевт", now=base, tz=MSK, limit=5000, horizon_days=14
        )
        runs.append(clock.perf_counter() - started)
    return statistics.median(runs)


def ruff_issues(path: Path) -> dict[str, int]:
    out = subprocess.run(  # noqa: S603 - фиксированная команда
        [
            str(RUFF),
            "check",
            str(path),
            "--isolated",
            "--select",
            "E,F,W,B,S,UP,SIM,C90,PL,RUF",
            "--line-length",
            "120",
            "--output-format",
            "json",
            "--exit-zero",
        ],
        capture_output=True,
        encoding="utf-8",
        check=False,
    )
    counts: dict[str, int] = {}
    for item in json.loads(out.stdout or "[]"):
        counts[item["code"]] = counts.get(item["code"], 0) + 1
    return counts


def main() -> None:
    rows = []
    paths = [HERE / "reference_slots.py", *sorted(HERE.glob("solutions/*/iter*/slots.py"))]
    for path in paths:
        name = "эталон" if path.name == "reference_slots.py" else f"{path.parts[-3]}/{path.parts[-2]}"
        lines = [
            ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip() and not ln.strip().startswith("#")
        ]
        issues = ruff_issues(path)
        try:
            seconds = f"{bench(load(path)):.3f}"
        except Exception as exc:  # noqa: BLE001 - метрика, а не проверка
            seconds = f"ошибка: {type(exc).__name__}"
        rows.append(
            (name, len(lines), sum(issues.values()), ", ".join(f"{k}×{v}" for k, v in sorted(issues.items())), seconds)
        )
    print("| Решение | Строк кода | Замечаний ruff | Коды | Время, с |")
    print("|---|---:|---:|---|---:|")
    for row in rows:
        print("| " + " | ".join(str(x) for x in row) + " |")


if __name__ == "__main__":
    main()

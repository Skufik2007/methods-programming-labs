"""Запись и чтение результатов замеров: reports/bench/<сценарий>.json."""

from __future__ import annotations

import json
import platform
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from vitals.bench.procs import REPORTS

BENCH_DIR = REPORTS / "bench"
IMG_DIR = REPORTS / "img"


def environment() -> dict[str, Any]:
    import os

    return {"platform": platform.platform(), "python": sys.version.split()[0], "cpu_count": os.cpu_count(),
            "time": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}


def write_result(name: str, data: dict[str, Any]) -> Path:
    BENCH_DIR.mkdir(parents=True, exist_ok=True)
    path = BENCH_DIR / f"{name}.json"
    path.write_text(json.dumps({"scenario": name, "env": environment(), **data}, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return path


def read_result(name: str) -> dict[str, Any] | None:
    path = BENCH_DIR / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

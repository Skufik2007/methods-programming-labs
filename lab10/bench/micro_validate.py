"""Микробенчмарк валидации заказа Pydantic — пара к Go-бенчмарку BenchmarkValidate.

Меряет время одной валидации и пиковую память, выделяемую на неё (tracemalloc).
Сравнивать с:  cd go && go test -run '^$' -bench . -benchmem ./internal/orders/ ./internal/api/

    python bench/micro_validate.py
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import timeit
import tracemalloc
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "python"))

from app.models import OrderRequest  # noqa: E402 — путь к пакету добавляется выше


def main() -> None:
    date = (dt.datetime.now(dt.UTC).date() + dt.timedelta(days=3)).isoformat()
    raw = (ROOT / "bench" / "order.json").read_text(encoding="utf-8").replace("__DATE__", date)
    data = json.loads(raw)

    # Две формы: из уже разобранного dict (как в Go-бенчмарке orders) и из сырого JSON
    # (как в обработчике: разбор + валидация).
    for title, fn in [
        ("model_validate(dict)", lambda: OrderRequest.model_validate(data)),
        ("model_validate_json(str)", lambda: OrderRequest.model_validate_json(raw)),
    ]:
        fn()  # прогрев
        timer = timeit.Timer(fn)
        loops, _ = timer.autorange()
        best = min(timer.repeat(repeat=5, number=loops)) / loops

        tracemalloc.start()
        tracemalloc.reset_peak()
        fn()
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        print(f"{title:26} {best * 1e6:8.2f} мкс/операция   пик памяти {peak:6d} Б/операция")


if __name__ == "__main__":
    main()

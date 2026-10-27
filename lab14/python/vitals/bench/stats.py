"""Чистые функции обработки результатов замеров (без процессов и ввода-вывода)."""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from typing import Any


def median(values: Iterable[float]) -> float:
    vals = list(values)
    if not vals:
        raise ValueError("медиана пустого набора")
    return float(statistics.median(vals))


def summarize_runs(
    runs: Sequence[Mapping[str, Any]], group_keys: Sequence[str], metrics: Sequence[str]
) -> list[dict[str, Any]]:
    """Сгруппировать прогоны по ключам и взять медиану каждой метрики.

    Возвращает список словарей: ключи группы, `runs` (число прогонов) и медианы метрик.
    Порядок групп — порядок первого появления.
    """
    groups: dict[tuple, list[Mapping[str, Any]]] = defaultdict(list)
    for r in runs:
        groups[tuple(r[k] for k in group_keys)].append(r)
    out = []
    for key, items in groups.items():
        row: dict[str, Any] = dict(zip(group_keys, key, strict=True))
        row["runs"] = len(items)
        for m in metrics:
            row[m] = median(i[m] for i in items)
        out.append(row)
    return out


def rate(count: int | float, seconds: float) -> float:
    return count / seconds if seconds > 0 else 0.0


def reduction_ratio(raw: float, agg: float) -> float:
    """Во сколько раз agg меньше raw (raw / agg); 0, если agg пуст."""
    return raw / agg if agg else 0.0


def speedup(slow: float, fast: float) -> float:
    return slow / fast if fast else 0.0


def readings_comparable(go_count: int, py_count: int) -> bool:
    """Оба сборщика должны собрать хоть что-то (без этого сравнение скорости бессмысленно)."""
    return go_count > 0 and py_count > 0


def common_prefix_identical(a: Sequence[str], b: Sequence[str]) -> bool:
    """Общий префикс двух последовательностей строк одного устройства совпадает посимвольно."""
    n = min(len(a), len(b))
    return n > 0 and list(a[:n]) == list(b[:n])

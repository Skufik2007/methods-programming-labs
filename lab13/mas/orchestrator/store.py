"""Хранилище запусков, реестр агентов и счётчики оркестратора."""

from __future__ import annotations

import logging
from collections import Counter, OrderedDict
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from mas.common.models import Heartbeat

log = logging.getLogger("mas.store")

MAX_RUNS = 500
ONLINE_TTL_S = 15.0

Listener = Callable[[dict[str, Any]], None]


@dataclass
class StepRecord:
    name: str
    kind: str = ""
    status: str = "pending"  # pending | running | completed | failed | skipped
    attempts: int = 0
    agent_id: str = ""
    output: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    started_at: str = ""
    finished_at: str = ""


@dataclass
class RunRecord:
    run_id: str
    kind: str  # case | feedback
    status: str = "running"  # running | completed | stopped | failed
    case: dict[str, Any] = field(default_factory=dict)
    steps: list[StepRecord] = field(default_factory=list)
    trace_id: str = ""
    error: str = ""
    recommendation: str = ""
    started_at: str = ""
    finished_at: str = ""

    def step(self, name: str) -> StepRecord:
        for rec in self.steps:
            if rec.name == name:
                return rec
        rec = StepRecord(name=name)
        self.steps.append(rec)
        return rec

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class RunStore(Protocol):
    def add(self, run: RunRecord) -> None: ...
    def touch(self, run: RunRecord) -> None: ...
    def get(self, run_id: str) -> RunRecord | None: ...
    def list(self, limit: int = 50) -> list[RunRecord]: ...
    def add_listener(self, listener: Listener) -> Callable[[], None]: ...


class InMemoryRunStore:
    """Последние `max_runs` запусков в памяти; слушатели получают снимок запуска при каждом изменении."""

    def __init__(self, max_runs: int = MAX_RUNS) -> None:
        self._max = max_runs
        self._runs: OrderedDict[str, RunRecord] = OrderedDict()
        self._listeners: list[Listener] = []

    def add(self, run: RunRecord) -> None:
        self._runs[run.run_id] = run
        while len(self._runs) > self._max:
            self._runs.popitem(last=False)
        self.touch(run)

    def touch(self, run: RunRecord) -> None:
        snapshot = run.to_dict()
        for listener in list(self._listeners):
            try:
                listener(snapshot)
            except Exception:
                log.exception("слушатель хранилища упал")

    def get(self, run_id: str) -> RunRecord | None:
        return self._runs.get(run_id)

    def list(self, limit: int = 50) -> list[RunRecord]:
        """Новые запуски первыми."""
        return list(reversed(self._runs.values()))[:limit]

    def add_listener(self, listener: Listener) -> Callable[[], None]:
        self._listeners.append(listener)

        def remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return remove


class AgentRegistry:
    """Реестр агентов по heartbeat; «в сети» — если последний heartbeat моложе 15 с (по времени приёма)."""

    def __init__(self, ttl_s: float = ONLINE_TTL_S, clock: Callable[[], datetime] | None = None) -> None:
        self._ttl = ttl_s
        self._clock = clock or (lambda: datetime.now(UTC))
        self._agents: dict[str, tuple[Heartbeat, datetime]] = {}

    def update(self, hb: Heartbeat) -> None:
        self._agents[hb.agent_id] = (hb, self._clock())

    def snapshot(self) -> list[dict[str, Any]]:
        now = self._clock()
        rows = []
        for hb, seen in self._agents.values():
            age = (now - seen).total_seconds()
            row = hb.model_dump()
            row["online"] = age < self._ttl and hb.status != "stopping"
            row["age_s"] = round(age, 1)
            rows.append(row)
        return sorted(rows, key=lambda r: (r["role"], r["agent_id"]))

    def online(self) -> list[dict[str, Any]]:
        return [row for row in self.snapshot() if row["online"]]


class Metrics:
    """Счётчики: запуски по статусам, задачи, повторы, таймауты."""

    def __init__(self) -> None:
        self.runs_by_status: Counter[str] = Counter()
        self.tasks = 0
        self.retries = 0
        self.timeouts = 0
        self.auctions = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "runs_by_status": dict(self.runs_by_status),
            "tasks": self.tasks,
            "retries": self.retries,
            "timeouts": self.timeouts,
            "auctions": self.auctions,
        }

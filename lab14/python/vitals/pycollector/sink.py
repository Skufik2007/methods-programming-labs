"""Сток files (OUT_DIR/raw|agg/<collector_id>-<YYYYMMDDHH>.jsonl) и файловое хранилище чекпоинтов.

Методы блокирующие и вызываются через asyncio.to_thread (fsync не должен стопорить цикл событий).
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO


class _Out:
    def __init__(self) -> None:
        self.hour = ""
        self.f: BinaryIO | None = None


class FilesSink:
    def __init__(self, out_dir: Path, collector_id: str, now: Callable[[], datetime]) -> None:
        self.dir = out_dir
        self.id = collector_id
        self._now = now
        self._outs = {"raw": _Out(), "agg": _Out()}
        self.bytes = {"raw": 0, "agg": 0}
        self.records = {"raw": 0, "agg": 0}

    def _open(self, kind: str) -> BinaryIO:
        o = self._outs[kind]
        hour = self._now().astimezone(UTC).strftime("%Y%m%d%H")
        if o.f is not None and o.hour == hour:
            return o.f
        if o.f is not None:  # смена часа
            o.f.close()
        folder = self.dir / kind
        folder.mkdir(parents=True, exist_ok=True)
        o.f = open(folder / f"{self.id}-{hour}.jsonl", "ab")  # noqa: SIM115 - закрывается в close()
        o.hour = hour
        return o.f

    def write_and_sync(self, kind: str, lines: list[str]) -> None:
        """Записать строки и fsync: чекпоинт двигается только после этого."""
        if not lines:
            return
        data = "".join(lines).encode("utf-8")
        f = self._open(kind)
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
        self.bytes[kind] += len(data)
        self.records[kind] += len(lines)

    def close(self) -> None:
        for o in self._outs.values():
            if o.f is not None:
                o.f.close()
                o.f = None


class FileCheckpoints:
    """Чекпоинты OUT_DIR/checkpoints.json; запись атомарная, значение только растёт."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._m: dict[str, int] = {}
        if path.exists():
            self._m = {k: int(v) for k, v in json.loads(path.read_text(encoding="utf-8")).items()}

    def load(self, device: str) -> int:
        return self._m.get(device, 0)

    def save(self, seqs: dict[str, int]) -> None:
        for d, v in seqs.items():
            if v > self._m.get(d, 0):
                self._m[d] = v
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._m, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, self.path)

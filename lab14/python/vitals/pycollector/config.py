"""Настройки сборщика из переменных окружения (имена и умолчания как у Go-сборщика)."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

_DUR_RE = re.compile(r"(\d+(?:\.\d+)?)(ms|us|µs|s|m|h)")
_UNITS = {"ms": 0.001, "us": 1e-6, "µs": 1e-6, "s": 1.0, "m": 60.0, "h": 3600.0}


def parse_duration(text: str) -> float:
    """Длительность в стиле Go ("500ms", "2s", "1m30s") -> секунды. Голое число = секунды."""
    s = text.strip()
    try:
        return float(s)
    except ValueError:
        pass
    parts = _DUR_RE.findall(s)
    if not parts or "".join(n + u for n, u in parts) != s:
        raise ValueError(f"ожидается длительность вида 500ms, 2s, 1m; получено {text!r}")
    return sum(float(n) * _UNITS[u] for n, u in parts)


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    collector_id: str = "collector-1"
    sim_url: str = "http://localhost:8080"
    device_list: tuple[str, ...] = ()
    workers: int = 16
    poll_interval: float = 1.0
    fetch_limit: int = 1000
    buffer: int = 10000
    batch_size: int = 500
    flush_interval: float = 2.0
    window: float = 10.0
    window_lateness: float = 2.0
    emit: str = "both"
    out_dir: str = "data"
    status_addr: str = ":9100"
    http_timeout: float = 10.0
    shutdown_timeout: float = 15.0
    sinks: tuple[str, ...] = field(default=("files",))

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Config:
        errors: list[str] = []

        def get(name: str, default: str) -> str:
            return env.get(name, "").strip() or default

        def integer(name: str, default: int, lo: int, hi: int) -> int:
            raw = get(name, str(default))
            try:
                v = int(raw)
            except ValueError:
                errors.append(f"{name}: ожидается целое число, получено {raw!r}")
                return default
            if not lo <= v <= hi:
                errors.append(f"{name}: значение {v} вне диапазона [{lo}, {hi}]")
                return default
            return v

        def duration(name: str, default: float, lo: float) -> float:
            raw = env.get(name, "").strip()
            if not raw:
                return default
            try:
                v = parse_duration(raw)
            except ValueError as exc:
                errors.append(f"{name}: {exc}")
                return default
            if v < lo:
                errors.append(f"{name}: значение {v}с меньше минимума {lo}с")
                return default
            return v

        cfg = cls(
            collector_id=get("COLLECTOR_ID", "collector-1"),
            sim_url=get("SIM_URL", "http://localhost:8080").rstrip("/"),
            device_list=tuple(d.strip() for d in env.get("DEVICE_LIST", "").split(",") if d.strip()),
            workers=integer("WORKERS", 16, 1, 4096),
            poll_interval=duration("POLL_INTERVAL", 1.0, 0.01),
            fetch_limit=integer("FETCH_LIMIT", 1000, 1, 1000),
            buffer=integer("BUFFER", 10000, 1, 100_000_000),
            batch_size=integer("BATCH_SIZE", 500, 1, 10_000_000),
            flush_interval=duration("FLUSH_INTERVAL", 2.0, 0.001),
            window=duration("WINDOW", 10.0, 1.0),
            window_lateness=duration("WINDOW_LATENESS", 2.0, 0.0),
            emit=get("EMIT", "both"),
            out_dir=get("OUT_DIR", "data"),
            status_addr=get("STATUS_ADDR", ":9100"),
            http_timeout=duration("HTTP_TIMEOUT", 10.0, 0.1),
            shutdown_timeout=duration("SHUTDOWN_TIMEOUT", 15.0, 1.0),
            sinks=tuple(s.strip() for s in get("SINKS", "files").split(",") if s.strip()),
        )
        if cfg.window != int(cfg.window):
            errors.append(f"WINDOW: должно быть кратно секунде, получено {cfg.window}с")
        if cfg.emit not in ("raw", "agg", "both"):
            errors.append(f"EMIT: допустимо raw, agg или both; получено {cfg.emit!r}")
        if cfg.sinks != ("files",):
            errors.append(f"SINKS: Python-сборщик поддерживает только files, получено {','.join(cfg.sinks)!r}")
        if get("COORD", "static") != "static":
            errors.append("COORD: Python-сборщик поддерживает только static")
        if errors:
            raise ConfigError("\n".join(errors))
        return cfg

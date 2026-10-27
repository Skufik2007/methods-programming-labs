"""Настройки из переменных окружения. Окружение читается только в `Settings.from_env`, не при импорте."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    log_level: str = "INFO"
    nats_url: str = "nats://localhost:4222"
    etcd_url: str = "http://localhost:2379"
    etcd_prefix: str = "/lab14/"
    data_dir: str = "data"
    out_dir: str = "data/out"
    flight_addrs: tuple[str, ...] = ()

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if environ is None else environ

        def get(name: str, default: str) -> str:
            value = env.get(name, "").strip()
            return value or default

        flight = tuple(a.strip() for a in env.get("FLIGHT_ADDRS", "").split(",") if a.strip())
        return cls(
            log_level=get("LOG_LEVEL", cls.log_level).upper(),
            nats_url=get("NATS_URL", cls.nats_url),
            etcd_url=get("ETCD_URL", cls.etcd_url),
            etcd_prefix=get("ETCD_PREFIX", cls.etcd_prefix),
            data_dir=get("DATA_DIR", cls.data_dir),
            out_dir=get("OUT_DIR", cls.out_dir),
            flight_addrs=flight,
        )

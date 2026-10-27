"""Настройки сервиса из переменных окружения."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name}: ожидается число, получено {raw!r}") from exc
    if value < 0:
        raise ValueError(f"{name}: значение не может быть отрицательным")
    return value


@dataclass(frozen=True)
class Settings:
    go_api_url: str = "http://127.0.0.1:8080"
    jwt_issuer: str = "lab10-go-api"
    jwt_audience: str = "lab10"
    jwks_cache_ttl: float = 300.0
    upstream_timeout: float = 5.0
    host: str = "127.0.0.1"
    port: int = 8000
    shutdown_timeout: float = 20.0
    drain_delay: float = 0.0
    log_level: str = "info"

    @property
    def jwks_url(self) -> str:
        return f"{self.go_api_url.rstrip('/')}/.well-known/jwks.json"

    @classmethod
    def from_env(cls) -> Settings:
        port_raw = os.getenv("PORT", "8000")
        if not port_raw.isdigit() or not 0 < int(port_raw) < 65536:
            raise ValueError(f"PORT: ожидается номер порта, получено {port_raw!r}")
        return cls(
            go_api_url=os.getenv("GO_API_URL", cls.go_api_url),
            jwt_issuer=os.getenv("JWT_ISSUER", cls.jwt_issuer),
            jwt_audience=os.getenv("JWT_AUDIENCE", cls.jwt_audience),
            jwks_cache_ttl=_env_float("JWKS_CACHE_TTL", cls.jwks_cache_ttl),
            upstream_timeout=_env_float("UPSTREAM_TIMEOUT", cls.upstream_timeout),
            host=os.getenv("HOST", cls.host),
            port=int(port_raw),
            shutdown_timeout=_env_float("SHUTDOWN_TIMEOUT", cls.shutdown_timeout),
            drain_delay=_env_float("DRAIN_DELAY", cls.drain_delay),
            log_level=os.getenv("LOG_LEVEL", cls.log_level).lower(),
        )

"""Точка входа: python -m app."""

from __future__ import annotations

import logging
import sys

from .config import Settings
from .main import create_app
from .server import build_server


def main() -> int:
    try:
        settings = Settings.from_env()
    except ValueError as exc:
        print(f"конфигурация: {exc}", file=sys.stderr)
        return 2

    logging.basicConfig(level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    server = build_server(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        shutdown_timeout=settings.shutdown_timeout,
        drain_delay=settings.drain_delay,
        log_level=settings.log_level,
    )
    server.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())

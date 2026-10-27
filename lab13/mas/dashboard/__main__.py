"""Запуск: `python -m mas.dashboard` (хост/порт — DASHBOARD_HOST/DASHBOARD_PORT, по умолчанию 127.0.0.1:8000)."""

from __future__ import annotations

import sys

import uvicorn

from mas.common.logging import setup_logging
from mas.dashboard.app import create_app
from mas.dashboard.settings import get_dashboard_settings


def main() -> int:
    settings = get_dashboard_settings()
    setup_logging(settings.log_level, settings.log_file)
    # Приложение создаётся здесь, а не при импорте; NATS и оркестратор поднимаются в lifespan.
    app = create_app(settings)
    uvicorn.run(app, host=settings.dashboard_host, port=settings.dashboard_port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())

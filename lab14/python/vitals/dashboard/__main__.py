"""python -m vitals.dashboard - веб-сервер. Окружение: NATS_URL, LOG_LEVEL, DASHBOARD_HOST, DASHBOARD_PORT."""

from __future__ import annotations

import argparse
import os

import uvicorn

from vitals.common import Settings, configure_logging
from vitals.dashboard.app import create_app


def main() -> None:
    p = argparse.ArgumentParser(prog="vitals.dashboard")
    p.add_argument("--host", default=os.environ.get("DASHBOARD_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("DASHBOARD_PORT", "8000")))
    args = p.parse_args()
    settings = Settings.from_env()
    configure_logging(settings.log_level)
    uvicorn.run(create_app(settings), host=args.host, port=args.port, log_config=None)


if __name__ == "__main__":
    main()

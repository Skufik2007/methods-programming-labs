"""python -m vitals.pycollector [--duration 30s]. Конфигурация — переменные окружения (как у Go-сборщика)."""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
from collections.abc import Sequence

from vitals.common import configure_logging
from vitals.pycollector.collector import Collector
from vitals.pycollector.config import Config, ConfigError, parse_duration


async def _amain(cfg: Config, duration: float) -> int:
    collector = Collector(cfg)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, collector.request_stop)
        except NotImplementedError:  # Windows: loop.add_signal_handler недоступен
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(collector.request_stop))
    return await collector.run(duration)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m vitals.pycollector")
    parser.add_argument("--duration", "-duration", default="0",
                        help="завершиться через указанное время (10s, 1m); 0 — до сигнала")
    args = parser.parse_args(argv)
    configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
    try:
        cfg = Config.from_env(os.environ)
        duration = parse_duration(args.duration)
    except (ConfigError, ValueError) as exc:
        print("ошибка конфигурации:\n" + str(exc), file=sys.stderr)
        return 2
    try:
        return asyncio.run(_amain(cfg, duration))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())

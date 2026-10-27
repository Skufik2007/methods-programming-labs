"""python -m vitals.streaming - потребитель JetStream. Конфигурация: окружение (NATS_URL, LOG_LEVEL) и флаги."""

from __future__ import annotations

import argparse
import asyncio
import signal

from vitals.common import Settings, configure_logging, get_logger
from vitals.streaming.analyzer import Analyzer
from vitals.streaming.consumer import run


async def amain(args: argparse.Namespace, settings: Settings) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows: обработчики через signal.signal
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))
    await run(args.nats_url or settings.nats_url, Analyzer(), stop, stats_interval=args.stats_interval)


def main() -> None:
    p = argparse.ArgumentParser(prog="vitals.streaming")
    p.add_argument("--nats-url", default="", help="по умолчанию NATS_URL")
    p.add_argument("--stats-interval", type=float, default=2.0, help="период vitals.stats, с")
    args = p.parse_args()
    settings = Settings.from_env()
    configure_logging(settings.log_level)
    get_logger("vitals.streaming").info("старт", extra={"nats": args.nats_url or settings.nats_url})
    asyncio.run(amain(args, settings))


if __name__ == "__main__":
    main()

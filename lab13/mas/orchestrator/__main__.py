"""CLI оркестратора: `python -m mas.orchestrator demo --cases N [--concurrency K]`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from mas.common.bus import NatsBus
from mas.common.logging import setup_logging
from mas.common.settings import get_settings
from mas.orchestrator.demo import format_table, run_demo
from mas.orchestrator.orchestrator import Orchestrator

log = logging.getLogger("mas.orchestrator.cli")


async def _demo(cases: int, concurrency: int) -> int:
    settings = get_settings()
    bus = NatsBus(settings.nats_url, name="orchestrator-demo")
    await bus.connect()
    orch = Orchestrator(bus, settings)
    try:
        await bus.ensure_stream()
        await orch.start()
        runs = await run_demo(orch, cases, concurrency)
        print(format_table(runs))
        print("Метрики:", orch.metrics.to_dict())
        return 1 if any(r.status == "failed" for r in runs) else 0
    finally:
        await orch.stop()
        await bus.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m mas.orchestrator")
    sub = parser.add_subparsers(dest="command", required=True)
    demo = sub.add_parser("demo", help="отправить N демо-обращений и вывести таблицу статусов")
    demo.add_argument("--cases", type=int, default=5)
    demo.add_argument("--concurrency", type=int, default=5)
    args = parser.parse_args(argv)

    settings = get_settings()
    setup_logging(settings.log_level, settings.log_file)
    try:
        return asyncio.run(_demo(args.cases, args.concurrency))
    except KeyboardInterrupt:
        return 130
    except OSError as exc:
        log.error("не удалось подключиться к NATS (%s): %s", settings.nats_url, exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())

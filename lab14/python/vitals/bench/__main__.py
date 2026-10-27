"""python -m vitals.bench <collectors|transport|raw_vs_agg|validation|report|all> [--quick]."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m vitals.bench")
    p.add_argument("scenario", choices=["collectors", "transport", "raw_vs_agg", "validation", "report", "all"])
    p.add_argument("--quick", action="store_true", help="уменьшенный режим (один размер, 1 прогон, короткие прогоны)")
    p.add_argument("--devices", type=int, nargs="+", help="collectors: размеры; transport/raw_vs_agg: первое значение")
    p.add_argument("--latency", type=int, nargs="+", help="collectors: LATENCY_MS")
    p.add_argument("--repeats", type=int)
    p.add_argument("--duration", type=float, help="collectors: секунд на прогон; transport: секунд накопления")
    p.add_argument("--workers", type=int, default=16)
    p.add_argument("--windows", type=int, nargs="+", help="raw_vs_agg: значения WINDOW, с")
    p.add_argument("--collector", choices=["go", "python"], default="go", help="raw_vs_agg: какой сборщик")
    p.add_argument("--rows", type=int, default=1_000_000, help="validation")
    a = p.parse_args(argv)

    todo = ["collectors", "transport", "raw_vs_agg", "validation", "report"] if a.scenario == "all" else [a.scenario]
    for name in todo:
        print(f"== {name} ==", flush=True)
        if name == "collectors":
            from vitals.bench import collectors
            collectors.run(a.quick, a.devices, a.latency, a.repeats, a.duration, a.workers)
        elif name == "transport":
            from vitals.bench import transport
            transport.run(a.quick, a.devices[0] if a.devices else None, a.duration, a.repeats)
        elif name == "raw_vs_agg":
            from vitals.bench import raw_vs_agg
            raw_vs_agg.run(a.quick, a.devices[0] if a.devices else None, a.windows, a.collector)
        elif name == "validation":
            from vitals.bench import validation
            validation.run(a.quick, a.rows, a.repeats)
        else:
            from vitals.bench import report
            report.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())

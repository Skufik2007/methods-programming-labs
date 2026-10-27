"""Командная строка: python -m vitals.analytics run --source files|flight --in <dir> --out <dir>."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from vitals.common import Settings, configure_logging


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m vitals.analytics")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="полный прогон ETL")
    run.add_argument("--source", choices=["files", "flight"], default="files")
    run.add_argument("--in", dest="in_dir", type=Path, help="каталог с raw/ и agg/ (для files)")
    run.add_argument("--out", dest="out_dir", type=Path, help="каталог результатов (по умолчанию OUT_DIR или data/out)")
    run.add_argument("--flight", action="append", default=[], help="адрес Flight-сервера, можно несколько раз")
    run.add_argument("--repeats", type=int, default=5, help="число прогонов для медианы времени")
    run.add_argument("--no-plots", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings.from_env()
    configure_logging(settings.log_level)
    from vitals.analytics.pipeline import run  # импорт здесь: тяжёлые библиотеки не нужны для --help

    flight = args.flight or list(settings.flight_addrs)
    result = run(
        source=args.source,
        in_dir=args.in_dir or (Path(settings.data_dir) if args.source == "files" else None),
        out_dir=args.out_dir or Path(settings.out_dir),
        flight=flight,
        repeats=args.repeats,
        with_plots=not args.no_plots,
    )
    print(f"готово: {result['raw_rows']} -> {result['clean_rows']} строк, графиков: {len(result['images'])}")
    return 0

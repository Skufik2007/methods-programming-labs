"""Сквозная проверка стенда `compose.yaml` (запускается после `docker compose up -d --build --wait`).

Проверяет поведение, а не «контейнеры поднялись»:
  1. у обоих сборщиков есть непустые raw- и agg-файлы с корректными JSON-строками;
  2. устройства поделены: множества по /status не пересекаются, объединение = все устройства эмулятора;
  3. в NATS JetStream есть сообщения;
  4. дашборд отдаёт непустой /api/snapshot;
  5. Arrow Flight DoGet у обоих сборщиков возвращает строки;
  6. после остановки collector-2 collector-1 забирает все его устройства и продолжает читать.

Любой провал даёт ненулевой код возврата: ни одна проверка не засчитывается «по умолчанию».
Зависимости: pyarrow (для Flight); остальное — стандартная библиотека.
"""

from __future__ import annotations

import argparse
import json
import subprocess  # noqa: S404 - вызываем docker compose, это назначение скрипта
import sys
import time
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
failures: list[str] = []


def log(msg: str) -> None:
    print(f"[e2e {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def http_json(url: str, timeout: float = 5.0) -> Any:
    if not url.startswith("http://"):
        raise ValueError(url)
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - схема проверена выше
        return json.loads(resp.read())


def compose(*args: str, timeout: float = 120.0) -> str:
    cmd = ["docker", "compose", *args]
    done = subprocess.run(  # noqa: S603 - аргументы фиксированы скриптом
        cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout, check=False, encoding="utf-8"
    )
    if done.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} -> {done.returncode}: {done.stderr.strip()}")
    return done.stdout


def wait_for(name: str, probe: Callable[[], str | None], timeout: float, interval: float = 2.0) -> bool:
    """probe возвращает None при успехе или строку с причиной «пока не готово». Итог пишется в failures."""
    deadline = time.monotonic() + timeout
    reason = "не вызывалась"
    while True:
        try:
            result = probe()
        except Exception as exc:  # noqa: BLE001 - причина идёт в сообщение, проверка повторится
            result = f"{type(exc).__name__}: {exc}"
        if result is None:
            log(f"OK   {name}")
            return True
        reason = result
        if time.monotonic() >= deadline:
            break
        time.sleep(interval)
    log(f"FAIL {name}: {reason}")
    failures.append(f"{name}: {reason}")
    return False


def device_ids(sim_url: str) -> set[str]:
    return {d["device_id"] for d in http_json(f"{sim_url}/v1/devices")["devices"]}


def count_lines(service: str, kind: str) -> int:
    out = compose("exec", "-T", service, "sh", "-c", f"cat /data/{kind}/*.jsonl 2>/dev/null | wc -l")
    return int(out.strip() or 0)


def first_line(service: str, kind: str) -> dict[str, Any]:
    out = compose("exec", "-T", service, "sh", "-c", f"cat /data/{kind}/*.jsonl 2>/dev/null | head -n 1")
    return json.loads(out)


def check_files(service: str) -> str | None:
    required = {
        "raw": {"device_id", "patient_id", "seq", "ts", "metric", "value", "unit", "battery"},
        "agg": {"window_start", "window_end", "device_id", "metric", "count", "invalid", "collector_id"},
    }
    for kind, keys in required.items():
        n = count_lines(service, kind)
        if n <= 0:
            return f"{service}: /data/{kind}/*.jsonl пуст или отсутствует"
        missing = keys - first_line(service, kind).keys()
        if missing:
            return f"{service}: в {kind}-строке нет полей {sorted(missing)}"
    return None


def check_partition(args: argparse.Namespace) -> str | None:
    all_ids = device_ids(args.sim)
    sets: dict[str, set[str]] = {}
    for name, url in (("collector-1", args.status1), ("collector-2", args.status2)):
        st = http_json(f"{url}/status")
        sets[name] = set(st["devices"])
        if st["devices_assigned"] != len(sets[name]):
            return f"{name}: devices_assigned={st['devices_assigned']}, но в списке {len(sets[name])} (список усечён?)"
    a, b = sets["collector-1"], sets["collector-2"]
    if not a or not b:
        return f"у сборщика нет устройств: collector-1={len(a)}, collector-2={len(b)}"
    if a & b:
        return f"пересечение устройств: {sorted(a & b)[:5]} (всего {len(a & b)})"
    if (a | b) != all_ids:
        return f"не все устройства назначены: {len(a | b)} из {len(all_ids)}"
    return None


def check_nats(url: str) -> str | None:
    info = http_json(f"{url}/jsz")
    if int(info.get("streams", 0)) < 1:
        return "в JetStream нет потоков"
    if int(info.get("messages", 0)) <= 0:
        return "в JetStream нет сообщений"
    return None


def check_dashboard(url: str) -> str | None:
    snap = http_json(f"{url}/api/snapshot")
    if not snap.get("patients"):
        return f"snapshot пуст: ts={snap.get('ts')}"
    return None


def check_flight(addr: str) -> str | None:
    import pyarrow.flight as flight  # импорт здесь: остальные проверки не требуют pyarrow

    since = (datetime.now(UTC) - timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    for kind in ("raw", "agg"):
        ticket = flight.Ticket(json.dumps({"kind": kind, "since": since}).encode())
        with flight.connect(f"grpc://{addr}") as client:
            table = client.do_get(ticket).read_all()
        if table.num_rows <= 0:
            return f"Flight {addr}: DoGet {kind} вернул 0 строк"
    return None


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--wait", type=float, default=60.0, help="сколько секунд даём данным накопиться (по умолчанию 60)")
    p.add_argument("--sim", default="http://localhost:8080")
    p.add_argument("--status1", default="http://localhost:9101")
    p.add_argument("--status2", default="http://localhost:9102")
    p.add_argument("--nats-mon", default="http://localhost:8222")
    p.add_argument("--dashboard", default="http://localhost:8000")
    p.add_argument("--flight1", default="localhost:8815")
    p.add_argument("--flight2", default="localhost:8816")
    args = p.parse_args()

    log(f"жду {args.wait:.0f} с, пока данные накопятся во всех стоках")
    time.sleep(args.wait)
    step = 10.0  # после накопления даём ещё немного времени на запаздывающие стоки

    for svc in ("collector-1", "collector-2"):
        wait_for(f"файлы raw/agg у {svc}", lambda svc=svc: check_files(svc), step)
    wait_for("устройства поделены без пересечений", lambda: check_partition(args), step)
    wait_for("NATS JetStream содержит сообщения", lambda: check_nats(args.nats_mon), step)
    wait_for("дашборд: /api/snapshot не пуст", lambda: check_dashboard(args.dashboard), 30.0)
    for addr in (args.flight1, args.flight2):
        wait_for(f"Arrow Flight DoGet {addr}", lambda addr=addr: check_flight(addr), step)

    # --- перехват устройств при остановке одного сборщика ---
    total = len(device_ids(args.sim))
    before = http_json(f"{args.status1}/status")["readings"]
    log("останавливаю collector-2 (graceful: отзовёт lease, и collector-1 перехватит его устройства)")
    try:
        compose("stop", "-t", "25", "collector-2", timeout=60.0)
    except RuntimeError as exc:
        failures.append(f"остановка collector-2: {exc}")
    else:

        def took_over() -> str | None:
            st = http_json(f"{args.status1}/status")
            if st["devices_assigned"] != total:
                return f"collector-1 владеет {st['devices_assigned']} из {total} устройств"
            return None

        if wait_for("collector-1 забрал все устройства", took_over, 60.0):

            def reading() -> str | None:
                now = http_json(f"{args.status1}/status")["readings"]
                return None if now > before else f"readings не растёт: было {before}, стало {now}"

            wait_for("collector-1 продолжает читать показания", reading, 30.0)

    if failures:
        log(f"ПРОВАЛЕНО проверок: {len(failures)}")
        for f in failures:
            log(f"  - {f}")
        return 1
    log("все проверки пройдены")
    return 0


if __name__ == "__main__":
    sys.exit(main())

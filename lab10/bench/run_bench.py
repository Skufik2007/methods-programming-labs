"""Нагрузочное сравнение Gin и FastAPI с замером памяти (повышенное задание №6).

Что делает:
  1. собирает Go-сервис и генератор нагрузки go/cmd/loadgen;
  2. запускает оба сервиса как отдельные процессы с выключенным логом запросов;
  3. замеряет RSS в простое, затем для каждого сценария и уровня параллельности
     гоняет loadgen и раз в 50 мс снимает RSS процесса (с дочерними);
  4. печатает таблицы и сохраняет их в bench/results/<платформа>.{md,json}.

Запуск из корня репозитория:
    python bench/run_bench.py                    # полный прогон, ~2 минуты
    python bench/run_bench.py --duration 2 --concurrency 1 16   # быстро
    python bench/run_bench.py --py-workers 4     # FastAPI в 4 процессах uvicorn
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os
import platform
import shutil
import socket
import statistics
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Self

import httpx
import psutil

ROOT = Path(__file__).resolve().parent.parent
TMP = ROOT / "bench" / "tmp"
RESULTS = ROOT / "bench" / "results"
EXE = ".exe" if sys.platform == "win32" else ""


@dataclass
class Service:
    name: str
    url: str
    proc: subprocess.Popen[bytes]
    idle_rss_mb: float = 0.0
    idle_uss_mb: float = 0.0
    peak_rss_mb: float = 0.0
    after_rss_mb: float = 0.0
    after_uss_mb: float = 0.0


@dataclass
class Run:
    scenario: str
    concurrency: int
    service: str
    rps: float
    p50_ms: float
    p99_ms: float
    errors: int
    non_2xx: int
    peak_rss_mb: float


@dataclass
class Report:
    started_at: str
    environment: dict[str, Any]
    params: dict[str, Any]
    runs: list[Run] = field(default_factory=list)
    memory: dict[str, dict[str, float]] = field(default_factory=dict)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def go_build(target: str) -> Path:
    out = TMP / f"{target}{EXE}"
    subprocess.run(["go", "build", "-o", str(out), f"./cmd/{target}"], cwd=ROOT / "go", check=True)
    return out


def tree_rss_mb(proc: psutil.Process) -> float:
    """RSS процесса и всех его потомков (воркеры uvicorn), в МБ."""
    total = 0
    for p in [proc, *proc.children(recursive=True)]:
        with contextlib.suppress(psutil.NoSuchProcess):  # воркер мог завершиться между вызовами
            total += p.memory_info().rss
    return total / 2**20


def tree_uss_mb(proc: psutil.Process) -> float:
    """USS — память, принадлежащая только процессу (без общих библиотек ОС).

    RSS включает разделяемые страницы DLL/so и поэтому завышает «цену» процесса;
    USS честнее для сравнения. Считается дольше, поэтому не используется для пика.
    """
    total = 0
    for p in [proc, *proc.children(recursive=True)]:
        with contextlib.suppress(psutil.NoSuchProcess):
            total += p.memory_full_info().uss
    return total / 2**20


def wait_ready(url: str, timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{url}/health/ready", timeout=1).status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(0.1)
    raise RuntimeError(f"{url} не поднялся за {timeout} с")


def start_go(binary: Path) -> Service:
    port = free_port()
    env = {**os.environ, "ADDR": f"127.0.0.1:{port}", "LOG_LEVEL": "warn"}
    proc = subprocess.Popen([str(binary)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return Service("Gin (Go)", f"http://127.0.0.1:{port}", proc)


def start_python(workers: int) -> Service:
    port = free_port()
    env = {**os.environ, "PORT": str(port), "LOG_LEVEL": "warning", "PYTHONUNBUFFERED": "1"}
    if workers > 1:
        cmd = [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:create_app",
            "--factory",
            "--port",
            str(port),
            "--workers",
            str(workers),
            "--log-level",
            "warning",
            "--no-access-log",
        ]
    else:
        cmd = [sys.executable, "-m", "app"]
    proc = subprocess.Popen(cmd, cwd=ROOT / "python", env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    label = "FastAPI (Python)" if workers == 1 else f"FastAPI (Python, {workers} воркера)"
    return Service(label, f"http://127.0.0.1:{port}", proc)


class Sampler:
    """Фоновый замер пикового RSS во время прогона."""

    def __init__(self, proc: psutil.Process, interval: float = 0.05) -> None:
        self.proc = proc
        self.interval = interval
        self.peak = 0.0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.peak = max(self.peak, tree_rss_mb(self.proc))
            self._stop.wait(self.interval)

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join()


def idle_rss(proc: psutil.Process, seconds: float = 1.0) -> float:
    samples = []
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        samples.append(tree_rss_mb(proc))
        time.sleep(0.05)
    return statistics.median(samples)


def load(loadgen: Path, url: str, conc: int, duration: float, body: Path | None) -> dict[str, Any]:
    cmd = [str(loadgen), "-url", url, "-c", str(conc), "-d", f"{duration}s", "-warmup", "1s"]
    if body is not None:
        cmd += ["-method", "POST", "-body", str(body)]
    out = subprocess.run(cmd, check=True, capture_output=True, text=True)
    return json.loads(out.stdout)


def render(report: Report) -> str:
    lines = [
        "# Результаты нагрузочного сравнения Gin и FastAPI",
        "",
        f"- Дата: {report.started_at}",
        "- Окружение: " + ", ".join(f"{k}: {v}" for k, v in report.environment.items()),
        f"- Параметры: {report.params}",
        "",
        "## Пропускная способность и задержки",
        "",
        "| Сценарий | Параллельность | Сервис | RPS | p50, мс | p99, мс | Ошибки | Пик RSS, МБ |",
        "|---|---:|---|---:|---:|---:|---:|---:|",
    ]
    for r in report.runs:
        rps = f"{r.rps:,.0f}".replace(",", " ")  # разделитель тысяч — пробел
        lines.append(
            f"| {r.scenario} | {r.concurrency} | {r.service} | {rps} | {r.p50_ms:.2f} | {r.p99_ms:.2f} "
            f"| {r.errors + r.non_2xx} | {r.peak_rss_mb:.1f} |"
        )
    lines += [
        "",
        "## Память (RSS процесса вместе с дочерними)",
        "",
        "| Сервис | RSS в простое, МБ | USS в простое, МБ | Пик RSS под нагрузкой, МБ "
        "| RSS после нагрузки, МБ | USS после нагрузки, МБ |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for name, m in report.memory.items():
        lines.append(
            f"| {name} | {m['idle']:.1f} | {m['idle_uss']:.1f} | {m['peak']:.1f} "
            f"| {m['after']:.1f} | {m['after_uss']:.1f} |"
        )
    lines += [
        "",
        "RSS — вся резидентная память процесса, включая разделяемые страницы системных библиотек;",
        "USS — только собственная память процесса. Пик снимается каждые 50 мс по RSS.",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--duration", type=float, default=5, help="секунд на каждый замер")
    parser.add_argument("--concurrency", type=int, nargs="+", default=[1, 16, 64])
    parser.add_argument("--py-workers", type=int, default=1, help="число процессов uvicorn")
    parser.add_argument("--output", default=None, help="имя файла результатов без расширения")
    args = parser.parse_args()

    if shutil.which("go") is None:
        print("нужен компилятор Go", file=sys.stderr)
        return 2
    TMP.mkdir(parents=True, exist_ok=True)
    RESULTS.mkdir(parents=True, exist_ok=True)

    body = TMP / "order.json"
    date = (dt.datetime.now(dt.UTC).date() + dt.timedelta(days=3)).isoformat()
    body.write_text(
        (ROOT / "bench" / "order.json").read_text(encoding="utf-8").replace("__DATE__", date), encoding="utf-8"
    )

    print("сборка Go-сервиса и генератора нагрузки...")
    server_bin, loadgen = go_build("server"), go_build("loadgen")

    go_version = subprocess.run(["go", "version"], capture_output=True, text=True, check=True).stdout.split()[2]
    report = Report(
        started_at=dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        environment={
            "ОС": f"{platform.system()} {platform.release()}",
            "CPU": f"{psutil.cpu_count(logical=True)} логических ядер",
            "Go": go_version,
            "Python": platform.python_version(),
        },
        params={"duration_s": args.duration, "concurrency": args.concurrency, "py_workers": args.py_workers},
    )
    scenarios = [("GET /ping", "/ping", None), ("POST /api/v1/orders/validate", "/api/v1/orders/validate", body)]

    services = [start_go(server_bin), start_python(args.py_workers)]
    try:
        for s in services:
            wait_ready(s.url)
        # Пауза, чтобы после запуска улеглись разовые выделения (генерация RSA-ключа, импорты).
        time.sleep(3)
        for s in services:
            ps = psutil.Process(s.proc.pid)
            s.idle_rss_mb, s.idle_uss_mb = idle_rss(ps), tree_uss_mb(ps)
            print(f"{s.name}: в простое RSS {s.idle_rss_mb:.1f} МБ, USS {s.idle_uss_mb:.1f} МБ")

        for title, path, payload in scenarios:
            for conc in args.concurrency:
                for s in services:
                    with Sampler(psutil.Process(s.proc.pid)) as sampler:
                        res = load(loadgen, s.url + path, conc, args.duration, payload)
                    s.peak_rss_mb = max(s.peak_rss_mb, sampler.peak)
                    run = Run(
                        title,
                        conc,
                        s.name,
                        res["rps"],
                        res["latency_ms"]["p50"],
                        res["latency_ms"]["p99"],
                        res["errors"],
                        res["non_2xx"],
                        sampler.peak,
                    )
                    report.runs.append(run)
                    print(
                        f"{title:32} c={conc:<3} {s.name:28} {run.rps:10.0f} rps  p99 {run.p99_ms:7.2f} мс  "
                        f"пик {run.peak_rss_mb:6.1f} МБ"
                    )

        time.sleep(2)
        for s in services:
            ps = psutil.Process(s.proc.pid)
            s.after_rss_mb, s.after_uss_mb = idle_rss(ps), tree_uss_mb(ps)
            report.memory[s.name] = {
                "idle": s.idle_rss_mb,
                "idle_uss": s.idle_uss_mb,
                "peak": s.peak_rss_mb,
                "after": s.after_rss_mb,
                "after_uss": s.after_uss_mb,
            }
    finally:
        for s in services:
            s.proc.terminate()
            try:
                s.proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                s.proc.kill()

    name = args.output or f"{platform.system().lower()}" + (f"-py{args.py_workers}" if args.py_workers > 1 else "")
    (RESULTS / f"{name}.json").write_text(json.dumps(asdict(report), ensure_ascii=False, indent=2), encoding="utf-8")
    md = render(report)
    (RESULTS / f"{name}.md").write_text(md, encoding="utf-8")
    print("\n" + md)
    print(f"сохранено: bench/results/{name}.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())

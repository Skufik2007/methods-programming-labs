"""Запуск и измерение внешних процессов: эмулятор, Go- и Python-сборщик.

Остановка сборщиков — флагом `--duration` (сигналы на Windows ненадёжны); эмулятор не имеет
такого флага, его останавливаем terminate().
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parents[3]
GO_DIR = ROOT / "go"
BIN_DIR = GO_DIR / "bin"
REPORTS = ROOT / "reports"
SAMPLE_PERIOD = 0.2  # опрос процесса psutil раз в 200 мс
_EXE = ".exe" if os.name == "nt" else ""


def bin_path(name: str) -> Path:
    return BIN_DIR / f"{name}{_EXE}"


def find_go() -> str | None:
    found = shutil.which("go")
    if found:
        return found
    for cand in (Path(r"D:\dev\go\bin") / "go.exe", Path("/usr/local/go/bin/go")):
        if cand.exists():
            return str(cand)
    return None


def ensure_go_binaries() -> None:
    """Собрать go/bin/{sensorsim,collector}, если их нет (`go build -o bin/ ./cmd/...` из каталога go)."""
    if bin_path("sensorsim").exists() and bin_path("collector").exists():
        return
    go = find_go()
    if go is None:
        raise RuntimeError("нет go/bin/*.exe и не найден компилятор Go (PATH или D:\\dev\\go\\bin)")
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    subprocess.run([go, "build", "-o", str(BIN_DIR) + os.sep, "./cmd/..."], cwd=GO_DIR, check=True)  # noqa: S603


def free_port(start: int) -> int:
    """Первый свободный TCP-порт начиная с `start` (чужие порты не занимаем)."""
    for port in range(start, start + 200):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError(f"нет свободных портов от {start}")


def rfc3339(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _wait_http(url: str, timeout: float = 15.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            with urllib.request.urlopen(url, timeout=1):  # noqa: S310 - локальный адрес
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"{url} не отвечает за {timeout} с")


@contextmanager
def simulator(port: int, devices: int, latency_ms: int, backlog_hours: float = 0.0, seed: int = 1):
    """Эмулятор на 127.0.0.1:port. backlog_hours > 0 — START в прошлом: у сборщика есть что вычитывать."""
    ensure_go_binaries()
    env = {**os.environ, "ADDR": f"127.0.0.1:{port}", "DEVICES": str(devices), "LATENCY_MS": str(latency_ms),
           "SEED": str(seed)}
    if backlog_hours > 0:
        env["START"] = rfc3339(datetime.now(UTC) - timedelta(hours=backlog_hours))
    proc = subprocess.Popen([str(bin_path("sensorsim"))], env=env, stdout=subprocess.DEVNULL,  # noqa: S603
                            stderr=subprocess.DEVNULL)
    try:
        _wait_http(f"http://127.0.0.1:{port}/healthz")
        yield f"http://127.0.0.1:{port}"
    finally:
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()


@dataclass
class Measured:
    exit_code: int
    wall_s: float
    peak_rss_mb: float
    cpu_s: float  # user + system процесса и его потомков

    @property
    def cpu_pct(self) -> float:
        """Загрузка в процентах одного ядра (100 = ядро занято целиком)."""
        return 100.0 * self.cpu_s / self.wall_s if self.wall_s > 0 else 0.0


def run_measured(cmd: list[str], env: Mapping[str, str], timeout: float) -> Measured:
    """Запустить процесс до завершения, раз в 200 мс снимая RSS и CPU (процесс + потомки).

    Потомки учитываются, потому что venv-python на Windows — лаунчер, порождающий настоящий интерпретатор.
    """
    t0 = time.monotonic()
    proc = subprocess.Popen(cmd, env=dict(env), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # noqa: S603
    root = psutil.Process(proc.pid)
    peak = 0
    cpu_by_pid: dict[int, float] = {}
    stop = threading.Event()

    def sample() -> None:
        nonlocal peak
        while not stop.is_set():
            try:
                procs = [root, *root.children(recursive=True)]
            except psutil.Error:
                break
            rss = 0
            for p in procs:
                try:
                    rss += p.memory_info().rss
                    t = p.cpu_times()
                    cpu_by_pid[p.pid] = t.user + t.system  # последнее увиденное значение процесса
                except psutil.Error:
                    continue
            peak = max(peak, rss)
            stop.wait(SAMPLE_PERIOD)

    th = threading.Thread(target=sample, daemon=True)
    th.start()
    try:
        code = proc.wait(timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        code = -1
    wall = time.monotonic() - t0
    stop.set()
    th.join(2)
    return Measured(code, wall, peak / 2**20, sum(cpu_by_pid.values()))


def collector_command(kind: str, duration_s: float) -> list[str]:
    if kind == "go":
        return [str(bin_path("collector")), f"-duration={duration_s:g}s"]
    if kind == "python":
        return [sys.executable, "-m", "vitals.pycollector", f"--duration={duration_s:g}s"]
    raise ValueError(kind)


def collector_env(sim_url: str, out_dir: Path, status_port: int, *, workers: int = 16, emit: str = "raw",
                  window: int = 10, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    env = {
        **os.environ,
        "SIM_URL": sim_url, "OUT_DIR": str(out_dir), "STATUS_ADDR": f"127.0.0.1:{status_port}",
        "WORKERS": str(workers), "EMIT": emit, "WINDOW": f"{window}s", "SINKS": "files", "COORD": "static",
        "SHUTDOWN_TIMEOUT": "20s",
        "PYTHONPATH": str(ROOT / "python") + os.pathsep + os.environ.get("PYTHONPATH", ""),
        "PYTHONUTF8": "1",
    }
    env.update(extra or {})
    return env


def count_lines(directory: Path) -> int:
    """Число строк во всех *.jsonl каталога (блоками, без разбора)."""
    total = 0
    for f in sorted(directory.glob("*.jsonl")):
        with open(f, "rb") as fh:
            while chunk := fh.read(1 << 22):
                total += chunk.count(b"\n")
    return total


def dir_bytes(directory: Path) -> int:
    return sum(f.stat().st_size for f in directory.glob("*.jsonl"))

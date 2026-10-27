"""Общие функции: сборка Go-программ и работа с сетевыми портами."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GO_DIR = ROOT / "go"
BIN_DIR = ROOT / "bin"
EXE_SUFFIX = ".exe" if sys.platform == "win32" else ""


def go_binary(name: str) -> Path:
    return BIN_DIR / f"{name}{EXE_SUFFIX}"


_built: set[str] = set()


def build_go(name: str) -> Path:
    """Компилирует go/cmd/<name> в bin/<name>.

    `go build` вызывается всегда (один раз за процесс): у Go свой кэш сборки,
    он сам определяет, что изменилось, включая общие пакеты и зависимости.
    """
    binary = go_binary(name)
    if name in _built and binary.exists():
        return binary

    go = shutil.which("go")
    if go is None:
        raise RuntimeError("компилятор Go не найден в PATH — установите Go: https://go.dev/dl/")

    BIN_DIR.mkdir(exist_ok=True)
    subprocess.run(
        [go, "build", "-o", str(binary), f"./cmd/{name}"],
        cwd=GO_DIR,
        check=True,
    )
    _built.add(name)
    return binary


def free_port() -> int:
    """Свободный TCP-порт на localhost (ОС выдаёт его при bind на порт 0)."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for_port(host: str, port: int, timeout: float = 10.0) -> None:
    """Ждёт, пока сервер начнёт принимать соединения."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.05)
    raise TimeoutError(f"сервер {host}:{port} не поднялся за {timeout} с")


def start_server(name: str, addr: str) -> subprocess.Popen:
    """Собирает и запускает Go-сервер, дожидаясь его готовности."""
    binary = build_go(name)
    proc = subprocess.Popen(
        [str(binary), "-addr", addr],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    host, port = addr.rsplit(":", 1)
    try:
        wait_for_port(host, int(port))
    except Exception:
        stop_server(proc)
        raise
    return proc


def stop_server(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def configure_stdout() -> None:
    """Включает UTF-8 вывод в консоли Windows, чтобы кириллица не превращалась в кракозябры."""
    if os.name == "nt":
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8")

"""Повышенное задание 1: Python-приложение, которое отдаёт тяжёлые вычисления
Go-микросервису по HTTP.

Используется только стандартная библиотека (urllib), чтобы не требовать установки пакетов.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from common import configure_stdout, free_port, start_server, stop_server


class ServiceError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


class ComputeClient:
    """Клиент Go-микросервиса вычислений."""

    def __init__(self, base_url: str, timeout: float = 120.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _call(self, method: str, path: str, body: dict | None = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"} if data else {},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            try:
                message = json.load(exc).get("error", exc.reason)
            except (json.JSONDecodeError, AttributeError):
                message = exc.reason
            raise ServiceError(exc.code, message) from None

    def health(self) -> dict:
        return self._call("GET", "/health")

    def count_primes(self, limit: int, workers: int | None = None) -> dict:
        query = f"/primes?limit={limit}" + (f"&workers={workers}" if workers else "")
        return self._call("GET", query)

    def matmul(self, size: int, seed: int = 0) -> dict:
        return self._call("POST", "/matmul", {"size": size, "seed": seed})


def main() -> None:
    configure_stdout()
    port = free_port()
    server = start_server("microservice", f"127.0.0.1:{port}")
    client = ComputeClient(f"http://127.0.0.1:{port}")
    try:
        print("health:", client.health())

        r = client.count_primes(50_000_000)
        print(f"простых до 50 млн: {r['count']} за {r['elapsed_ms']} мс ({r['workers']} горутин)")

        r = client.matmul(500, seed=42)
        print(f"матрицы 500x500: след = {r['trace']:.3f} за {r['elapsed_ms']} мс")

        # Несколько запросов параллельно: Python ждёт ответы в потоках,
        # а вычисления идут в горутинах Go-сервиса.
        limits = [5_000_000, 10_000_000, 20_000_000, 40_000_000]
        start = time.perf_counter()
        with ThreadPoolExecutor(max_workers=len(limits)) as pool:
            results = list(pool.map(client.count_primes, limits))
        total = time.perf_counter() - start
        print(f"\n{len(limits)} параллельных запроса за {total * 1000:.0f} мс:")
        for res in results:
            print(f"  limit={res['limit']:>11_}: {res['count']:>9_} простых")

        try:
            client.matmul(0)
        except ServiceError as exc:
            print("\nОшибка валидации обработана:", exc)
    finally:
        stop_server(server)


if __name__ == "__main__":
    main()

"""Задание 5: Python-клиент для TCP-сервера на Go.

Протокол: одна JSON-строка на запрос и одна JSON-строка на ответ.
"""

from __future__ import annotations

import json
import socket
from typing import Any

from common import configure_stdout, free_port, start_server, stop_server


class ServerError(RuntimeError):
    """Сервер ответил ошибкой."""


class TcpClient:
    def __init__(self, host: str, port: int, timeout: float = 5.0) -> None:
        self._sock = socket.create_connection((host, port), timeout=timeout)
        self._reader = self._sock.makefile("r", encoding="utf-8", newline="\n")

    def request(self, cmd: str, **params: Any) -> Any:
        line = json.dumps({"cmd": cmd, **params}, ensure_ascii=False) + "\n"
        self._sock.sendall(line.encode("utf-8"))

        reply = self._reader.readline()
        if not reply:
            raise ConnectionError("сервер закрыл соединение")
        resp = json.loads(reply)
        if not resp.get("ok"):
            raise ServerError(resp.get("error", "неизвестная ошибка"))
        return resp["result"]

    def close(self) -> None:
        """Корректно завершает сессию: request() отправляет quit и вычитывает ответ "bye"."""
        try:
            self.request("quit")
        except (OSError, ConnectionError, ServerError):
            pass
        self._reader.close()
        self._sock.close()

    def __enter__(self) -> "TcpClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def main() -> None:
    configure_stdout()
    port = free_port()
    server = start_server("tcpserver", f"127.0.0.1:{port}")
    print(f"Go TCP-сервер запущен на порту {port}")
    try:
        with TcpClient("127.0.0.1", port) as client:
            print("ping    ->", client.request("ping"))
            print("upper   ->", client.request("upper", text="привет из python"))
            print("reverse ->", client.request("reverse", text="Go и Python"))
            print("sum     ->", client.request("sum", numbers=[10, 20, 30]))
            print("time    ->", client.request("time"))
            try:
                client.request("unknown")
            except ServerError as exc:
                print("ошибка  ->", exc)
    finally:
        stop_server(server)


if __name__ == "__main__":
    main()

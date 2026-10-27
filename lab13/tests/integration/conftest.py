"""Интеграционные тесты с настоящим nats-server (локально — нативный бинарник, в CI — сервис).

Если сервер недоступен, тесты пропускаются: модульные тесты от него не зависят.
"""

import os
import socket
from urllib.parse import urlparse

import pytest


@pytest.fixture
def nats_url() -> str:
    url = os.environ.get("NATS_URL", "nats://localhost:4222")
    parsed = urlparse(url)
    try:
        socket.create_connection((parsed.hostname, parsed.port or 4222), timeout=0.5).close()
    except OSError:
        pytest.skip(f"NATS недоступен по {url}")
    return url

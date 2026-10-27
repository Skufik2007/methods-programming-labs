"""Асинхронный клиент Ollama (`POST /api/chat`).

Основа — клиент из lab12/.github/scripts/ai_review.py (urllib), переписан на httpx.AsyncClient:
те же `options` (temperature 0.2, num_predict, repeat_penalty 1.1 — маленькие модели зацикливаются),
проверка схемы URL и разбор ответа с понятными ошибками.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import httpx


class LLMError(RuntimeError):
    """Модель не ответила или вернула некорректный ответ."""


def require_http_url(url: str) -> str:
    if urlsplit(url).scheme not in {"http", "https"}:
        raise ValueError(f"разрешены только http(s)-адреса: {url!r}")
    return url


def build_chat_payload(
    model: str,
    messages: list[dict[str, str]],
    *,
    json_format: bool = False,
    temperature: float = 0.2,
    num_predict: int = 512,
    repeat_penalty: float = 1.1,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
        "keep_alive": "10m",
        "options": {"temperature": temperature, "num_predict": num_predict, "repeat_penalty": repeat_penalty},
    }
    if json_format:
        payload["format"] = "json"
    return payload


def parse_chat_response(data: Any) -> str:
    try:
        content = data["message"]["content"]
    except (KeyError, TypeError) as exc:
        raise LLMError(f"некорректный ответ Ollama: {exc!r}") from exc
    if not isinstance(content, str) or not content.strip():
        raise LLMError("модель вернула пустой ответ")
    return content.strip()


class OllamaClient:
    """Клиент создаёт httpx.AsyncClient лениво; `aclose()` обязателен при завершении."""

    def __init__(self, base_url: str, timeout_s: float, client: httpx.AsyncClient | None = None) -> None:
        self._url = require_http_url(f"{base_url.rstrip('/')}/api/chat")
        self._timeout = timeout_s
        self._client = client
        self._owns_client = client is None

    async def chat(self, payload: dict[str, Any]) -> str:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        try:
            response = await self._client.post(self._url, json=payload, timeout=self._timeout)
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError as exc:
            raise LLMError(f"Ollama вернула HTTP {exc.response.status_code}") from exc
        except httpx.TimeoutException as exc:
            raise LLMError(f"таймаут Ollama ({self._timeout:g} с)") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise LLMError(f"нет ответа от Ollama: {exc!r}") from exc
        return parse_chat_response(data)

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
        self._client = None

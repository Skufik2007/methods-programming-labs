"""Логи в виде JSON-строк (одна запись — одна строка) в UTF-8 на stderr."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import IO, Any

# Поля стандартной записи logging: всё остальное в record.__dict__ — это пользовательские `extra`.
_STD_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    """Форматирует запись как JSON; кириллица остаётся читаемой (ensure_ascii=False)."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str | int = "INFO", stream: IO[str] | None = None) -> None:
    """Настроить корневой логгер. Вызывается из main, не при импорте. Повторный вызов заменяет обработчик."""
    if stream is None:
        stream = sys.stderr
        # На Windows stderr по умолчанию в cp1251/cp866 — без этого кириллица в логах превращается в «?».
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    handler.set_name("vitals-json")
    root = logging.getLogger()
    root.handlers = [h for h in root.handlers if h.get_name() != "vitals-json"]
    root.addHandler(handler)
    root.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)

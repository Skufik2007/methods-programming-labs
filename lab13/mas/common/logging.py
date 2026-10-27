"""Логирование: JSON-строки в консоль и (по желанию) в файл."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime

_HANDLER_MARK = "_mas_handler"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = {
            "ts": datetime.fromtimestamp(record.created, UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            data.update(extra)
        if record.exc_info:
            data["exc"] = self.formatException(record.exc_info)
        return json.dumps(data, ensure_ascii=False, default=str)


def setup_logging(level: str = "INFO", log_file: str = "") -> logging.Logger:
    """Настраивает логгер `mas`; повторный вызов заменяет свои обработчики, а не дублирует."""
    logger = logging.getLogger("mas")
    for handler in list(logger.handlers):
        if getattr(handler, _HANDLER_MARK, False):
            logger.removeHandler(handler)
            handler.close()
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False
    formatter = JsonFormatter()
    # На Windows перенаправленный stderr открыт в кодировке ANSI (cp1251), а Go-агенты
    # пишут UTF-8: без перекодировки общий журнал из нескольких процессов не читается.
    if (sys.stderr.encoding or "").lower().replace("-", "") != "utf8" and hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    for handler in handlers:
        handler.setFormatter(formatter)
        setattr(handler, _HANDLER_MARK, True)
        logger.addHandler(handler)
    return logger

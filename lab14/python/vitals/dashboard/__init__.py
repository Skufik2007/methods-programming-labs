"""Дашборд реального времени: FastAPI + WebSocket поверх vitals.stats / vitals.alerts."""

from vitals.dashboard.app import create_app

__all__ = ["create_app"]

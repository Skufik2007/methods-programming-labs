"""Веб-панель мониторинга мультиагентной системы (FastAPI + Jinja2)."""

from mas.dashboard.app import create_app

__all__ = ["create_app"]

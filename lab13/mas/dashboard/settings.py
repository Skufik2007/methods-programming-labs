"""Настройки дашборда из переменных окружения (расширяют общие настройки процессов)."""

from __future__ import annotations

from mas.common.settings import Settings


class DashboardSettings(Settings):
    """Общие настройки (NATS, таймауты оркестратора) плюс параметры веб-панели.

    Оркестратор работает в том же процессе и получает этот же объект настроек.
    """

    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8000
    # Пустой адрес автомасштабировщика — блок на странице «Обзор» показывает «не настроен».
    autoscaler_url: str = ""
    jaeger_ui_url: str = "http://localhost:16686"
    # Сколько последних событий хранить в памяти (раздел 3 задания).
    max_events: int = 2000
    # Таймаут обращения к автомасштабировщику и к JetStream за статистикой очередей.
    probe_timeout_s: float = 1.5


def get_dashboard_settings() -> DashboardSettings:
    return DashboardSettings()

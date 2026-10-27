"""Находка №6: график «временной ряд пациента с эпизодами» всегда рисовал ЧСС и SpO2, а
выбирал пациента по любому эпизоду. У пациента с лихорадкой красная полоса закрывала
ЧСС и SpO2 без всяких отклонений — график вводил в заблуждение. Панели должны показывать
метрики, по которым у пациента были эпизоды."""

from datetime import UTC, datetime

import polars as pl
from vitals.analytics.plots import timeseries_panels


def _episodes(*alerts: str) -> pl.DataFrame:
    t = datetime(2026, 10, 13, tzinfo=UTC)
    return pl.DataFrame({"alert": list(alerts), "patient_id": ["p-1"] * len(alerts),
                         "start": [t] * len(alerts), "end": [t] * len(alerts)})


def test_fever_episode_shows_temperature() -> None:
    assert "temperature" in timeseries_panels(_episodes("fever"), "p-1")


def test_panels_follow_episode_metrics_and_keep_two_panels() -> None:
    assert timeseries_panels(_episodes("hypertension", "tachycardia"), "p-1") == ["systolic_bp", "heart_rate"]
    assert timeseries_panels(_episodes(), "p-1") == ["heart_rate", "spo2"]

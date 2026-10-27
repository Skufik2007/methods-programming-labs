"""Правила тревог (раздел 7 контракта) - чистые функции - и трекер состояния с гистерезисом."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from vitals.streaming.window import Agg


class PatientView(Protocol):
    """Что нужно правилам от окна: агрегат метрики за интервал и сводка по всем метрикам пациента."""

    def agg(self, metric: str, span_s: int) -> Agg | None: ...
    def total(self, span_s: int) -> Agg | None: ...


# Минимум показаний, чтобы правило имело смысл (одно выбросное значение не поднимает тревогу).
MIN_SAMPLES_MEAN = 3
MIN_SAMPLES_FAULT = 20


def rule_tachycardia(v: PatientView) -> float | None:
    """Среднее ЧСС за 1 мин > 120. Возвращает значение, вызвавшее тревогу, иначе None."""
    a = v.agg("heart_rate", 60)
    return a.mean if a and a.valid >= MIN_SAMPLES_MEAN and a.mean > 120 else None


def rule_desaturation(v: PatientView) -> float | None:
    """Минимум SpO2 за 1 мин < 92."""
    a = v.agg("spo2", 60)
    return a.min if a and a.valid >= 1 and a.min < 92 else None


def rule_fever(v: PatientView) -> float | None:
    """Последняя температура > 38.0 (смотрим последнюю минуту, чтобы не держать тревогу по старым данным)."""
    a = v.agg("temperature", 60)
    return a.last if a and a.valid >= 1 and a.last > 38.0 else None


def rule_hypertension(v: PatientView) -> float | None:
    """Среднее САД за 5 мин > 160."""
    a = v.agg("systolic_bp", 300)
    return a.mean if a and a.valid >= MIN_SAMPLES_MEAN and a.mean > 160 else None


def rule_sensor_fault(v: PatientView) -> float | None:
    """Доля невалидных показаний за 5 мин по всем метрикам пациента > 20%."""
    a = v.total(300)
    return a.invalid_ratio if a and a.count >= MIN_SAMPLES_FAULT and a.invalid_ratio > 0.20 else None


RULES: dict[str, Callable[[PatientView], float | None]] = {
    "tachycardia": rule_tachycardia,
    "desaturation": rule_desaturation,
    "fever": rule_fever,
    "hypertension": rule_hypertension,
    "sensor_fault": rule_sensor_fault,
}


@dataclass(frozen=True)
class AlertEvent:
    event: str  # "raised" | "cleared"
    patient_id: str
    alert: str
    ts: float
    value: float | None = None


class AlertTracker:
    """Состояние тревог с гистерезисом: поднимается сразу, снимается, когда условие ложно `clear_after_s` подряд.

    Событие выдаётся только при смене состояния - повторов каждую секунду нет.
    """

    def __init__(self, clear_after_s: float = 30.0) -> None:
        self.clear_after_s = clear_after_s
        # (patient, alert) -> момент, когда условие стало ложным (None - условие истинно сейчас)
        self._active: dict[tuple[str, str], float | None] = {}

    def update(self, patient_id: str, alert: str, value: float | None, now: float) -> AlertEvent | None:
        key = (patient_id, alert)
        if value is not None:
            was_active = key in self._active
            self._active[key] = None
            return None if was_active else AlertEvent("raised", patient_id, alert, now, value)
        if key not in self._active:
            return None
        false_since = self._active[key]
        if false_since is None:
            self._active[key] = now
            return None
        if now - false_since >= self.clear_after_s:
            del self._active[key]
            return AlertEvent("cleared", patient_id, alert, now)
        return None

    def active_for(self, patient_id: str) -> list[str]:
        return sorted(a for p, a in self._active if p == patient_id)

    def active(self) -> list[tuple[str, str]]:
        return sorted(self._active)

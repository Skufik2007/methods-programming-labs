"""Pydantic-модели сообщений контракта (docs/ARCHITECTURE.md, раздел 4 и далее)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from mas.common.specialties import SPECIALTIES


def utc_now_str(now: datetime | None = None) -> str:
    """RFC 3339 в UTC без долей секунды: 2026-10-13T09:30:00Z."""
    return (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def new_id() -> str:
    return str(uuid.uuid4())


class _Base(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Case(_Base):
    """Обращение пациента (4.3)."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    patient_id: str = Field(min_length=1, max_length=100)
    patient_name: str = Field(min_length=1, max_length=200)
    age: int = Field(ge=0, le=120)
    phone: str = ""
    complaint: str = Field(min_length=3, max_length=2000)
    preferred_specialty: str = ""

    @field_validator("preferred_specialty")
    @classmethod
    def _known_specialty(cls, value: str) -> str:
        if value and value not in SPECIALTIES:
            raise ValueError("неизвестная специальность")
        return value


class Task(_Base):
    """Задача (4.1)."""

    task_id: str = Field(default_factory=new_id)
    run_id: str
    step: str
    kind: str
    attempt: int = 1
    created_at: str = Field(default_factory=utc_now_str)
    payload: dict[str, Any] = Field(default_factory=dict)

    @property
    def msg_id(self) -> str:
        return f"{self.task_id}-{self.attempt}"


class Result(_Base):
    """Результат шага (4.2)."""

    task_id: str
    run_id: str
    step: str
    agent_id: str = ""
    success: bool
    output: dict[str, Any] = Field(default_factory=dict)
    error: str = ""
    retryable: bool = False
    duration_ms: int = 0
    finished_at: str = Field(default_factory=utc_now_str)


class IntakeOutput(_Base):
    """Выход intake (4.4); специальность проверяется по справочнику."""

    symptoms: list[str] = Field(default_factory=list, max_length=20)
    specialty: str
    red_flags: list[str] = Field(default_factory=list, max_length=20)
    summary: str = Field(default="", max_length=500)
    source: Literal["llm", "fallback"] = "llm"

    @field_validator("specialty")
    @classmethod
    def _known_specialty(cls, value: str) -> str:
        if value not in SPECIALTIES:
            raise ValueError(f"специальность вне справочника: {value!r}")
        return value

    @field_validator("symptoms", "red_flags")
    @classmethod
    def _clean_items(cls, value: list[str]) -> list[str]:
        return [item.strip()[:100] for item in value if isinstance(item, str) and item.strip()]


class TriageOutput(_Base):
    urgency: Literal["emergency", "urgent", "routine"]
    specialty: str = ""
    max_wait_hours: float = 72
    reasons: list[str] = Field(default_factory=list)
    recommendation: str = ""


class FeedbackReplyOutput(_Base):
    reply: str = Field(min_length=1, max_length=1500)
    source: Literal["llm", "fallback"] = "llm"


class AuctionRequest(_Base):
    """Запрос аукциона (раздел 6, п. 1)."""

    auction_id: str = Field(default_factory=new_id)
    run_id: str
    specialty: str
    urgency: str
    max_wait_hours: float
    now: str = Field(default_factory=utc_now_str)
    exclude_agents: list[str] = Field(default_factory=list)


class Bid(_Base):
    """Ставка агента записи (раздел 6, п. 2)."""

    auction_id: str
    agent_id: str
    doctor_id: str = ""
    slot_start: str = ""
    skill: float = 0.0
    availability: float = 0.0
    load: float = 0.0
    cost: float = 1.0
    eligible: bool = False
    reason: str = ""


class BookRequest(_Base):
    """Запрос брони победителю (раздел 6, п. 4)."""

    auction_id: str
    run_id: str
    doctor_id: str
    slot_start: str
    patient_id: str
    patient_name: str
    # Окно пациента: если слот уже заняли, агент берёт следующий слот того же врача в нём.
    max_wait_hours: float = 0.0


class Heartbeat(_Base):
    """Heartbeat агента (раздел 9)."""

    agent_id: str
    role: str = ""
    kind: str = ""
    title: str = ""
    status: str = "ready"
    processed: int = 0
    failed: int = 0
    inflight: int = 0
    started_at: str = ""
    sent_at: str = Field(default_factory=utc_now_str)
    host: str = ""
    lang: str = ""


class Event(_Base):
    """Событие журнала (раздел 10)."""

    ts: str = Field(default_factory=utc_now_str)
    source: str
    action: str
    run_id: str = ""
    task_id: str = ""
    step: str = ""
    trace_id: str = ""
    span_id: str = ""
    subject: str = ""
    summary: str = ""

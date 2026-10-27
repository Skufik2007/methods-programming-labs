"""Оркестратор через фейковую шину (разделы 2, 5, 6, 10): цепочка, повторы, аукцион, отзыв, журнал, реестр."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from opentelemetry.sdk.trace import TracerProvider
from pydantic import ValidationError

from mas.common.bus import BusMessage
from mas.common.models import Heartbeat, Result, Task
from mas.common.settings import Settings
from mas.orchestrator.orchestrator import Orchestrator
from mas.orchestrator.store import AgentRegistry, RunRecord
from tests.fakes import FakeBus

CASE = {
    "patient_id": "p-001",
    "patient_name": "Иванова Анна Петровна",
    "age": 34,
    "phone": "+7 900 000-00-01",
    "complaint": "Третий день температура 38, кашель, болит горло",
    "preferred_specialty": "",
}
INTAKE = {"symptoms": ["температура", "кашель"], "specialty": "therapist", "red_flags": [],
          "summary": "ОРВИ-подобные симптомы 3 дня", "source": "llm"}
TRIAGE = {"urgency": "routine", "specialty": "therapist", "max_wait_hours": 72,
          "reasons": ["температура без красных флагов"], "recommendation": "Запись к терапевту"}
EMERGENCY = {"urgency": "emergency", "specialty": "", "max_wait_hours": 0, "reasons": ["боль в груди"],
             "recommendation": "Вызовите скорую: 103"}
REMINDER = {"reminders": [{"reminder_id": "r-1", "send_at": "2026-10-14T06:00:00Z", "channel": "sms",
                           "status": "scheduled"}]}
SURVEY = {"survey_id": "s-1", "send_at": "2026-10-15T06:00:00Z", "channel": "sms"}
BOOKED = {"appointment_id": "a-1"}


def ok(output: dict[str, Any]):
    return lambda _task: (True, output)


def fail(error: str, *, retryable: bool = False):
    return lambda _task: (False, {}, {"error": error, "retryable": retryable})


def bid(agent: str, cost: float, *, eligible: bool = True, slot: str = "2026-10-14T06:00:00Z") -> dict[str, Any]:
    return {"agent_id": agent, "doctor_id": f"d-{agent}", "slot_start": slot, "skill": 1.0,
            "availability": 0.9, "load": 0.1, "cost": cost, "eligible": eligible, "reason": ""}


def happy_bus(**kwargs: Any) -> FakeBus:
    steps = {"intake": ok(INTAKE), "triage": ok(TRIAGE), "reminder": ok(REMINDER), "feedback": ok(SURVEY)}
    kwargs.setdefault("bids", [bid("booking-a-1", 0.3)])
    kwargs.setdefault("book", lambda _req: {"success": True, "output": BOOKED})
    return FakeBus(steps=steps, **kwargs)


def make_orch(bus: FakeBus, **settings: Any) -> Orchestrator:
    cfg = Settings(otel_sdk_disabled=True, **settings)
    return Orchestrator(bus, cfg, tracer=TracerProvider().get_tracer("test"))  # type: ignore[arg-type]


async def run_case(orch: Orchestrator, case: dict[str, Any] = CASE) -> RunRecord:
    await orch.start()
    try:
        run_id = await orch.start_case(case)
        return await orch.wait_run(run_id, timeout=5)
    finally:
        await orch.stop()


def published_steps(bus: FakeBus) -> list[str]:
    return [t.step for t in bus.published]


def events_for(bus: FakeBus, action: str) -> list[dict[str, Any]]:
    return [e for e in bus.events if e["action"] == action]


# ---- цепочка обращения (раздел 2) -------------------------------------------------------------------


async def test_full_chain_completes() -> None:
    bus = happy_bus()
    orch = make_orch(bus)
    run = await run_case(orch)
    assert run.status == "completed", run.error
    assert [s.name for s in run.steps] == ["intake", "triage", "booking", "reminder", "feedback"]
    assert all(s.status == "completed" for s in run.steps)
    assert run.finished_at
    assert run.error == ""


async def test_output_of_each_step_is_previous_of_next_step() -> None:
    bus = happy_bus()
    run = await run_case(make_orch(bus))
    assert run.status == "completed", run.error
    by_step = {t.step: t for t in bus.published}
    assert by_step["triage"].payload["previous"] == INTAKE
    assert by_step["reminder"].payload["previous"]["appointment_id"] == "a-1"
    assert by_step["feedback"].payload["previous"] == REMINDER


async def test_each_task_carries_case_context() -> None:
    bus = happy_bus()
    await run_case(make_orch(bus))
    assert bus.published, "задачи не опубликованы"
    for task in bus.published:
        assert task.payload["case"]["patient_name"] == CASE["patient_name"]
        assert task.payload["case"]["complaint"] == CASE["complaint"]


async def test_booking_goes_through_auction_not_task_queue() -> None:
    bus = happy_bus(bids=[bid("booking-a-1", 0.3), bid("booking-b-1", 0.2)])
    run = await run_case(make_orch(bus))
    assert run.status == "completed", run.error
    assert "booking" not in published_steps(bus)
    assert len(bus.book_requests) == 1
    req = bus.book_requests[0]
    assert req["doctor_id"] == "d-booking-b-1"  # минимальная стоимость
    assert req["patient_name"] == CASE["patient_name"]
    assert req["patient_id"] == CASE["patient_id"]
    booking = next(s for s in run.steps if s.name == "booking")
    assert booking.output["agent_id"] == "booking-b-1"
    assert booking.output["auction"] == {"bids": 2, "winner_cost": 0.2}


async def test_emergency_stops_chain_without_booking() -> None:
    bus = happy_bus()
    bus.steps["triage"] = ok(EMERGENCY)
    orch = make_orch(bus)
    run = await run_case(orch)
    assert run.status == "stopped"
    assert run.recommendation == "Вызовите скорую: 103"
    assert bus.book_requests == []
    assert published_steps(bus) == ["intake", "triage"]
    booking = next(s for s in run.steps if s.name == "booking")
    assert booking.status == "skipped"
    assert orch.metrics.runs_by_status["stopped"] == 1


async def test_emergency_writes_single_stopped_event_and_no_completed() -> None:
    bus = happy_bus()
    bus.steps["triage"] = ok(EMERGENCY)
    run = await run_case(make_orch(bus))
    stopped = [e for e in bus.events if e["run_id"] == run.run_id and e["action"] == "stopped"]
    assert len(stopped) == 1
    assert stopped[0]["summary"] == "Вызовите скорую: 103"
    finals = [e for e in bus.events if e["run_id"] == run.run_id and e["action"] in ("completed", "failed")]
    assert finals == []


# ---- повторы и таймауты (раздел 5) ------------------------------------------------------------------


async def test_timeout_is_retried_and_stale_task_discarded() -> None:
    bus = happy_bus()
    calls: list[Task] = []

    def triage_silent_then_ok(task: Task):
        calls.append(task)
        return None if len(calls) == 1 else (True, TRIAGE)

    bus.steps["triage"] = triage_silent_then_ok
    orch = make_orch(bus, step_timeout_s=0.05)
    run = await run_case(orch)
    assert run.status == "completed", run.error
    assert [t.attempt for t in calls] == [1, 2]
    assert orch.metrics.timeouts == 1
    assert orch.metrics.retries == 1
    assert len(bus.discarded) == 1  # задача первой попытки удалена из потока
    assert events_for(bus, "timeout") and events_for(bus, "retry")


async def test_retryable_error_is_retried() -> None:
    bus = happy_bus()
    calls: list[Task] = []

    def flaky(task: Task):
        calls.append(task)
        if len(calls) == 1:
            return (False, {}, {"error": "временная ошибка", "retryable": True})
        return (True, TRIAGE)

    bus.steps["triage"] = flaky
    orch = make_orch(bus)
    run = await run_case(orch)
    assert run.status == "completed", run.error
    assert len(calls) == 2
    retry = events_for(bus, "retry")
    assert retry and "временная ошибка" in retry[0]["summary"]


async def test_non_retryable_error_is_not_retried() -> None:
    bus = happy_bus()
    bus.steps["intake"] = fail("неверный формат обращения", retryable=False)
    orch = make_orch(bus)
    run = await run_case(orch)
    assert run.status == "failed"
    assert run.error == "intake: неверный формат обращения"
    assert published_steps(bus) == ["intake"]
    assert orch.metrics.retries == 0


async def test_at_most_three_attempts_for_retryable_error() -> None:
    bus = happy_bus()
    bus.steps["triage"] = fail("сбой агента", retryable=True)
    orch = make_orch(bus)
    run = await run_case(orch)
    assert run.status == "failed"
    assert published_steps(bus).count("triage") == 3
    triage = next(s for s in run.steps if s.name == "triage")
    assert triage.attempts == 3
    assert orch.metrics.retries == 2
    assert run.error.startswith("triage:")


async def test_at_most_three_attempts_for_timeouts() -> None:
    bus = happy_bus()
    bus.steps["triage"] = lambda _task: None  # агент молчит
    orch = make_orch(bus, step_timeout_s=0.02)
    run = await run_case(orch)
    assert run.status == "failed"
    assert published_steps(bus).count("triage") == 3
    assert orch.metrics.timeouts == 3


async def test_stale_result_from_previous_attempt_is_ignored() -> None:
    bus = happy_bus()
    attempts: list[Task] = []

    def triage(task: Task):
        attempts.append(task)
        if len(attempts) == 1:
            return None  # первая попытка молчит и истекает по таймауту
        if len(attempts) == 2:
            # Во время второй попытки приходит запоздалый результат ПЕРВОЙ (с её task_id).
            stale = Result(task_id=attempts[0].task_id, run_id=task.run_id, step="triage", success=True,
                           output={**TRIAGE, "urgency": "urgent"})
            asyncio.get_running_loop().call_soon(
                lambda: asyncio.ensure_future(bus.handlers["results.>"](
                    BusMessage(f"results.{task.run_id}", stale.model_dump_json().encode())))
            )
            return None  # вторая попытка тоже молчит
        return (True, TRIAGE)

    bus.steps["triage"] = triage
    orch = make_orch(bus, step_timeout_s=0.05)
    run = await run_case(orch)
    assert run.status == "completed", run.error
    assert len(attempts) == 3
    triage_step = next(s for s in run.steps if s.name == "triage")
    assert triage_step.output["urgency"] == "routine"  # запоздалый «urgent» не принят


# ---- аукцион: повторные раунды и отсутствие ставок (раздел 6) ----------------------------------------


async def test_slot_taken_starts_new_auction_round() -> None:
    replies = iter([{"success": False, "error": "slot_taken"}, {"success": True, "output": BOOKED}])
    bus = happy_bus(book=lambda _req: next(replies))
    orch = make_orch(bus)
    run = await run_case(orch)
    assert run.status == "completed", run.error
    assert len(bus.book_requests) == 2
    assert orch.metrics.auctions == 2
    booking = next(s for s in run.steps if s.name == "booking")
    assert booking.attempts == 2


async def test_slot_taken_three_times_fails_booking() -> None:
    bus = happy_bus(book=lambda _req: {"success": False, "error": "slot_taken"})
    orch = make_orch(bus)
    run = await run_case(orch)
    assert run.status == "failed"
    assert len(bus.book_requests) == 3
    assert orch.metrics.auctions == 3
    assert "slot_taken" in run.error


async def test_no_bids_stops_run_with_advice() -> None:
    # Правило изменено находкой №10: нет свободных врачей — это исход для пациента
    # (stopped с рекомендацией), а не сбой системы (failed).
    bus = happy_bus(bids=[])
    run = await run_case(make_orch(bus))
    assert run.status == "stopped"
    assert run.recommendation
    assert published_steps(bus) == ["intake", "triage"]
    assert bus.book_requests == []
    booking = next(s for s in run.steps if s.name == "booking")
    assert booking.status == "failed"


async def test_only_ineligible_bids_means_no_bids() -> None:
    bus = happy_bus(bids=[bid("booking-a-1", 0.1, eligible=False)])
    run = await run_case(make_orch(bus))
    assert run.status == "stopped"
    assert bus.book_requests == []


async def test_booking_timeout_excludes_agent_in_next_round() -> None:
    def book(req: dict[str, Any]):
        return None if req["doctor_id"] == "d-booking-a-1" else {"success": True,
                                                              "output": {"appointment_id": "a-2"}}

    bus = happy_bus(bids=[bid("booking-a-1", 0.2), bid("booking-b-1", 0.3)], book=book)
    run = await run_case(make_orch(bus))
    assert run.status == "completed", run.error
    assert [r["doctor_id"] for r in bus.book_requests] == ["d-booking-a-1", "d-booking-b-1"]
    booking = next(s for s in run.steps if s.name == "booking")
    assert booking.output["agent_id"] == "booking-b-1"


# ---- короткая цепочка отзыва (раздел 2) ---------------------------------------------------------------


async def test_negative_feedback_triggers_reply() -> None:
    analysis = {"rating": 2, "sentiment": "negative", "escalate": True, "topics": ["ожидание"],
                "nps_bucket": "detractor"}
    bus = FakeBus(steps={"feedback": ok(analysis), "feedback_reply": ok({"reply": "Спасибо", "source": "llm"})})
    orch = make_orch(bus)
    await orch.start()
    try:
        run_id = await orch.submit_feedback("a-1", "p-1", 2, "долго ждал приёма")
        run = await orch.wait_run(run_id, timeout=5)
    finally:
        await orch.stop()
    assert run.status == "completed", run.error
    assert published_steps(bus) == ["feedback", "feedback_reply"]
    assert [t.kind for t in bus.published] == ["analyze", "feedback_reply"]
    assert bus.published[1].payload["previous"]["topics"] == ["ожидание"]


async def test_positive_feedback_skips_reply() -> None:
    analysis = {"rating": 5, "sentiment": "positive", "escalate": False, "topics": [], "nps_bucket": "promoter"}
    bus = FakeBus(steps={"feedback": ok(analysis)})
    orch = make_orch(bus)
    await orch.start()
    try:
        run_id = await orch.submit_feedback("a-1", "p-1", 5, "всё отлично")
        run = await orch.wait_run(run_id, timeout=5)
    finally:
        await orch.stop()
    assert run.status == "completed", run.error
    assert published_steps(bus) == ["feedback"]


async def test_feedback_rating_must_be_one_to_five() -> None:
    orch = make_orch(FakeBus())
    for rating in (0, 6):
        with pytest.raises(ValueError, match="от 1 до 5"):
            await orch.submit_feedback("a-1", "p-1", rating, "текст")


async def test_invalid_case_is_rejected_before_run_starts() -> None:
    orch = make_orch(happy_bus())
    with pytest.raises(ValidationError):
        await orch.start_case({**CASE, "complaint": "ab"})
    assert orch.store.list() == []


async def test_unknown_run_raises_key_error() -> None:
    orch = make_orch(happy_bus())
    with pytest.raises(KeyError):
        await orch.wait_run("нет-такого")


# ---- журнал событий и метрики (раздел 10) -------------------------------------------------------------


async def test_successful_run_writes_journal_and_metrics() -> None:
    bus = happy_bus()
    orch = make_orch(bus)
    run = await run_case(orch)
    assert run.status == "completed", run.error
    run_events = [e for e in bus.events if e["run_id"] == run.run_id]
    actions = {e["action"] for e in run_events}
    assert {"published", "won", "completed"} <= actions
    assert all(e["source"] == "orchestrator" for e in run_events)
    won = events_for(bus, "won")
    assert won and "booking-a-1" in won[0]["summary"]
    published = [e for e in run_events if e["action"] == "published"]
    assert {e["step"] for e in published} == {"intake", "triage", "reminder", "feedback"}
    assert all(e["subject"] == f"tasks.{e['step']}" for e in published)

    metrics = orch.metrics.to_dict()
    assert metrics["runs_by_status"] == {"running": 0, "completed": 1}
    assert metrics["tasks"] == 4
    assert metrics["auctions"] == 1
    assert metrics["retries"] == 0
    assert metrics["timeouts"] == 0


async def test_failed_run_writes_failed_event_and_metrics() -> None:
    bus = happy_bus()
    bus.steps["intake"] = fail("сбой", retryable=False)
    orch = make_orch(bus)
    run = await run_case(orch)
    assert run.status == "failed"
    failed = [e for e in bus.events if e["run_id"] == run.run_id and e["action"] == "failed"]
    assert len(failed) == 1
    assert orch.metrics.runs_by_status["failed"] == 1
    assert orch.metrics.runs_by_status["running"] == 0


async def test_run_snapshot_is_listed_newest_first() -> None:
    orch = make_orch(happy_bus())
    await orch.start()
    try:
        first = await orch.start_case(CASE)
        await orch.wait_run(first, timeout=5)
        second = await orch.start_case(CASE)
        await orch.wait_run(second, timeout=5)
    finally:
        await orch.stop()
    assert [r.run_id for r in orch.store.list()] == [second, first]


async def test_queue_stats_empty_when_bus_cannot_report() -> None:
    orch = make_orch(FakeBus())
    assert await orch.queue_stats() == []


# ---- реестр агентов по heartbeat (раздел 9) ---------------------------------------------------------


async def test_heartbeat_message_registers_agent() -> None:
    bus = happy_bus()
    orch = make_orch(bus)
    await orch.start()
    try:
        hb = Heartbeat(agent_id="triage-1", role="triage", kind="triage", title="Триаж", lang="go")
        await bus.handlers["agents.heartbeat"](
            BusMessage("agents.heartbeat", hb.model_dump_json().encode()))
        assert [row["agent_id"] for row in orch.registry.online()] == ["triage-1"]
        await bus.handlers["agents.heartbeat"](BusMessage("agents.heartbeat", b"{not json"))
        assert len(orch.registry.snapshot()) == 1
    finally:
        await orch.stop()


def test_agent_is_online_while_heartbeat_younger_than_15_seconds() -> None:
    now = [0.0]
    base = 1_000_000.0

    from datetime import UTC, datetime

    def clock() -> datetime:
        return datetime.fromtimestamp(base + now[0], UTC)

    registry = AgentRegistry(clock=clock)
    registry.update(Heartbeat(agent_id="triage-1", role="triage", status="ready"))
    now[0] = 14.9
    assert [r["agent_id"] for r in registry.online()] == ["triage-1"]
    now[0] = 15.0
    assert registry.online() == []
    row = registry.snapshot()[0]
    assert row["online"] is False
    assert row["age_s"] == 15.0


def test_stopping_agent_is_offline_immediately() -> None:
    registry = AgentRegistry()
    registry.update(Heartbeat(agent_id="triage-1", role="triage", status="stopping"))
    assert registry.online() == []
    assert registry.snapshot()[0]["online"] is False


def test_later_heartbeat_refreshes_agent() -> None:
    now = [0.0]
    from datetime import UTC, datetime

    def clock() -> datetime:
        return datetime.fromtimestamp(1_000_000 + now[0], UTC)

    registry = AgentRegistry(clock=clock)
    registry.update(Heartbeat(agent_id="booking-1", role="booking"))
    now[0] = 10.0
    registry.update(Heartbeat(agent_id="booking-1", role="booking"))
    now[0] = 20.0  # с первого heartbeat прошло 20 с, со второго — 10 с
    assert [r["agent_id"] for r in registry.online()] == ["booking-1"]

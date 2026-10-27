"""Находка №10: под нагрузкой в CI 24 из 40 запусков завершились failed с «booking: no_bids».
Триаж даёт urgent с окном 4 ч, а невролог в клинике один: его слоты в окне быстро кончаются,
вечером их нет совсем. Пациент оставался без записи и без какой-либо рекомендации.

Правило (раздел 6 контракта, п. 3): плановое обращение — второй аукцион с окном, расширенным
в no_bids_extend_factor раз; если и так нет ставок или обращение срочное — запуск stopped
с понятной рекомендацией, а не failed (это не сбой системы)."""

from typing import Any

from tests.fakes import FakeBus
from tests.unit.test_orchestrator_chain import bid, make_orch, ok, run_case


def _bus(urgency: str, max_wait: int, bids: Any) -> FakeBus:
    steps = {
        "intake": ok({"symptoms": ["боль"], "specialty": "neurologist", "red_flags": [], "source": "llm"}),
        "triage": ok({"urgency": urgency, "specialty": "neurologist", "max_wait_hours": max_wait}),
        "reminder": ok({"reminders": []}),
        "feedback": ok({"survey_id": "s-1"}),
    }
    return FakeBus(steps=steps, bids=bids)


async def test_routine_without_slots_retries_with_wider_window() -> None:
    # Ставки появляются, только если окно шире исходных 72 ч.
    bus = _bus("routine", 72, lambda req: [bid("booking-b-1", 0.4)] if req["max_wait_hours"] > 72 else [])
    run = await run_case(make_orch(bus))
    assert run.status == "completed", run.error
    assert [a["max_wait_hours"] for a in bus.auctions] == [72, 288]


async def test_routine_without_slots_even_in_wider_window_is_stopped_with_advice() -> None:
    bus = _bus("routine", 72, [])
    run = await run_case(make_orch(bus))
    assert run.status == "stopped"
    assert "регистратур" in run.recommendation
    assert bus.book_requests == []


async def test_urgent_without_slots_is_stopped_with_urgent_care_advice() -> None:
    bus = _bus("urgent", 24, [])
    run = await run_case(make_orch(bus))
    assert run.status == "stopped"
    assert "неотложн" in run.recommendation
    assert len(bus.auctions) == 1  # срочного пациента не записываем на «через неделю»


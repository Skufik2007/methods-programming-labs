"""Находка №12: при одновременных аукционах выигранный слот часто уже занят. Агент записи
может взять следующий свободный слот того же врача, но только в пределах окна пациента —
поэтому оркестратор обязан передавать max_wait_hours в запросе брони."""

from tests.unit.test_orchestrator_chain import bid, happy_bus, make_orch, run_case


async def test_book_request_carries_patient_wait_window() -> None:
    bus = happy_bus(bids=[bid("booking-a-1", 0.2)])
    run = await run_case(make_orch(bus))
    assert run.status == "completed", run.error
    triage = next(s for s in run.steps if s.name == "triage")
    assert bus.book_requests[0]["max_wait_hours"] == triage.output["max_wait_hours"]

"""Находка №7: в выходе шага записи на дашборде было «аукцион: ставок 0, стоимость 0».
Агент записи заполняет поле auction из необязательных полей запроса брони (их оркестратор
не передаёт — получаются нули), а оркестратор дописывал свои данные через setdefault и
не перезаписывал их. Сведения об аукционе знает только оркестратор — он и должен их записать."""

from typing import Any

from opentelemetry.sdk.trace import TracerProvider

from mas.common.models import Task
from mas.common.settings import Settings
from mas.orchestrator.orchestrator import Orchestrator
from tests.fakes import FakeBus

ZERO_AUCTION = {"bids": 0, "winner_cost": 0}
CASE = {"patient_id": "p-1", "patient_name": "Иванова", "age": 30, "phone": "+7", "complaint": "болит горло"}


def _ok(output: dict[str, Any]):
    return lambda _task: (True, output)


def _bid(agent: str, cost: float) -> dict[str, Any]:
    return {"agent_id": agent, "doctor_id": f"d-{agent}", "slot_start": "2026-10-14T06:00:00Z", "skill": 1.0,
            "availability": 0.9, "load": 0.1, "cost": cost, "eligible": True, "reason": ""}


async def test_booking_output_has_real_auction_summary() -> None:
    steps = {
        "intake": _ok({"symptoms": ["кашель"], "specialty": "ent", "red_flags": [], "summary": "", "source": "llm"}),
        "triage": _ok({"urgency": "routine", "specialty": "ent", "max_wait_hours": 72}),
        "reminder": _ok({"reminders": []}),
        "feedback": _ok({"survey_id": "s-1"}),
    }
    bus = FakeBus(
        steps=steps,
        bids=[_bid("booking-a-1", 0.3), _bid("booking-b-1", 0.2)],
        # Так отвечает Go-агент записи: auction из необязательных полей запроса, по умолчанию нули.
        book=lambda req: {"success": True, "output": {"appointment_id": "a-1", "auction": ZERO_AUCTION}},
    )
    orch = Orchestrator(bus, Settings(otel_sdk_disabled=True), tracer=TracerProvider().get_tracer("t"))  # type: ignore[arg-type]
    await orch.start()
    run = await orch.wait_run(await orch.start_case(CASE), timeout=5)
    await orch.stop()
    assert run.status == "completed", run.error
    booking = next(s for s in run.steps if s.name == "booking")
    assert booking.output["auction"] == {"bids": 2, "winner_cost": 0.2}
    assert isinstance(bus.published[0], Task)

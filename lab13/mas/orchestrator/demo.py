"""Демо-нагрузка: набор правдоподобных обращений и итоговая таблица запусков."""

from __future__ import annotations

import asyncio
import random

from mas.orchestrator.orchestrator import Orchestrator
from mas.orchestrator.store import RunRecord

NAMES = (
    "Иванова Анна Петровна",
    "Петров Сергей Иванович",
    "Сидорова Мария Олеговна",
    "Кузнецов Дмитрий Алексеевич",
    "Смирнова Елена Викторовна",
    "Попов Андрей Николаевич",
)

COMPLAINTS = (
    ("Третий день температура 38, кашель, болит горло", ""),
    ("Давно беспокоят головные боли по вечерам и головокружение при вставании", ""),
    ("Периодически болит и слезится левый глаз, зрение стало хуже вдаль", ""),
    ("Появилась сыпь и сильный зуд на руках после смены стирального порошка", ""),
    ("Заложен нос и болит ухо уже пять дней, плохо слышу", ""),
    ("Подвернул ногу на тренировке, опухла лодыжка, больно наступать", ""),
    ("Перебои в сердце и повышенное давление по утрам", "cardiologist"),
    ("Слабость, ломота в теле, небольшая температура и насморк", ""),
)

# Одна жалоба с красным флагом: цепочка должна остановиться со статусом stopped.
RED_FLAG_COMPLAINT = "Сильная боль в груди отдаёт в левую руку, тяжело дышать, холодный пот"


def make_case(index: int, rng: random.Random | None = None) -> dict:
    rng = rng or random.Random(index)  # noqa: S311 - демо-данные, не криптография
    complaint, specialty = COMPLAINTS[index % len(COMPLAINTS)]
    if index == 2:
        complaint, specialty = RED_FLAG_COMPLAINT, ""
    return {
        "patient_id": f"p-{index + 1:03d}",
        "patient_name": NAMES[index % len(NAMES)],
        "age": rng.randint(18, 80),
        "phone": f"+7 900 000-{index + 1:04d}"[:16],
        "complaint": complaint,
        "preferred_specialty": specialty,
    }


async def run_demo(orch: Orchestrator, cases: int, concurrency: int, timeout: float = 600.0) -> list[RunRecord]:
    """Отправляет `cases` обращений не более чем `concurrency` одновременно и ждёт завершения всех."""
    sem = asyncio.Semaphore(max(1, concurrency))
    runs: list[RunRecord] = []

    async def one(i: int) -> None:
        async with sem:
            run_id = await orch.start_case(make_case(i))
            runs.append(await orch.wait_run(run_id, timeout))

    async with asyncio.TaskGroup() as group:
        for i in range(cases):
            group.create_task(one(i))
    return runs


def format_table(runs: list[RunRecord]) -> str:
    def cell(run: RunRecord, name: str) -> str:
        rec = next((s for s in run.steps if s.name == name), None)
        return "-" if rec is None else f"{rec.status[:4]}/{rec.attempts}"

    header = (
        f"{'run_id':<10} {'пациент':<8} {'статус':<10} {'intake':<8} {'triage':<8} "
        f"{'booking':<8} {'reminder':<9} причина"
    )
    lines = [header, "-" * len(header)]
    for run in sorted(runs, key=lambda r: r.started_at):
        reason = run.recommendation or run.error
        lines.append(
            f"{run.run_id[:8]:<10} {run.case.get('patient_id', ''):<8} {run.status:<10} "
            f"{cell(run, 'intake'):<8} {cell(run, 'triage'):<8} {cell(run, 'booking'):<8} "
            f"{cell(run, 'reminder'):<9} {reason}"
        )
    totals: dict[str, int] = {}
    for run in runs:
        totals[run.status] = totals.get(run.status, 0) + 1
    lines.append("")
    lines.append("Итого: " + ", ".join(f"{k}={v}" for k, v in sorted(totals.items())) + f" (всего {len(runs)})")
    return "\n".join(lines)

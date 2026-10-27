"""Оркестратор: цепочка шагов, повторы/таймауты, аукцион записи, трассировка."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import Span, Tracer

from mas.common.bus import MessageBus, Subscription, publish_event
from mas.common.models import (
    AuctionRequest,
    BookRequest,
    Case,
    Event,
    Heartbeat,
    Result,
    Task,
    new_id,
    utc_now_str,
)
from mas.common.settings import Settings, get_settings
from mas.common.telemetry import create_tracer_provider, current_ids
from mas.orchestrator.auction import choose_winner, parse_bids
from mas.orchestrator.store import AgentRegistry, InMemoryRunStore, Metrics, RunRecord, RunStore

log = logging.getLogger("mas.orchestrator")

SOURCE = "orchestrator"
EMERGENCY_RECOMMENDATION = "Вызовите скорую: 103"
NO_SLOTS_URGENT = (
    "Свободных приёмов в допустимый срок нет: обратитесь в кабинет неотложной помощи поликлиники "
    "или вызовите врача на дом"
)
NO_SLOTS_ROUTINE = "Свободных приёмов в ближайшие недели нет: регистратура свяжется с вами для записи"

# (шаг, kind задачи); booking выполняется аукционом, а не задачей JetStream.
CASE_CHAIN: tuple[tuple[str, str], ...] = (
    ("intake", "intake"),
    ("triage", "triage"),
    ("booking", "booking"),
    ("reminder", "reminder"),
    ("feedback", "schedule_survey"),
)


@dataclass
class StepOutcome:
    success: bool
    output: dict[str, Any]
    error: str = ""
    agent_id: str = ""


@dataclass
class _Pending:
    future: asyncio.Future[Result]
    run_id: str
    step: str
    attempt: int


def _fail(error: str) -> StepOutcome:
    return StepOutcome(False, {}, error)


class Orchestrator:
    """Публичный API: start/stop, start_case, submit_feedback, wait_run, а также
    поля store (запуски и подписка на изменения), registry (агенты) и metrics."""

    def __init__(
        self,
        bus: MessageBus,
        settings: Settings | None = None,
        *,
        tracer: Tracer | None = None,
        store: RunStore | None = None,
        registry: AgentRegistry | None = None,
        metrics: Metrics | None = None,
    ) -> None:
        self.bus = bus
        self.settings = settings or get_settings()
        self.store: RunStore = store or InMemoryRunStore()
        self.registry = registry or AgentRegistry()
        self.metrics = metrics or Metrics()
        self._own_tracer = tracer is None
        self._provider: TracerProvider | None = None
        self.tracer: Tracer = tracer or TracerProvider().get_tracer(SOURCE)
        self._pending: dict[str, _Pending] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._done: dict[str, asyncio.Event] = {}
        self._subs: list[Subscription] = []

    # ---- жизненный цикл ----------------------------------------------------

    async def start(self) -> None:
        if self._own_tracer:
            self._provider = create_tracer_provider(SOURCE, self.settings)
            self.tracer = self._provider.get_tracer(SOURCE)
        self._subs.append(await self.bus.subscribe("results.>", self._on_result))
        self._subs.append(await self.bus.subscribe("agents.heartbeat", self._on_heartbeat))

    async def stop(self) -> None:
        """Отменяет фоновые запуски и подписки. Шину закрывает тот, кто её создал."""
        for task in list(self._tasks.values()):
            task.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        for sub in self._subs:
            try:
                await sub.unsubscribe()
            except Exception:
                log.warning("не удалось отписаться", exc_info=True)
        self._subs.clear()
        if self._provider is not None:
            await asyncio.to_thread(self._provider.shutdown)
            self._provider = None

    # ---- публичные операции --------------------------------------------------

    async def start_case(self, case: Case | dict[str, Any]) -> str:
        """Запускает цепочку обращения в фоне, возвращает run_id. Невалидное обращение — ValidationError."""
        model = case if isinstance(case, Case) else Case.model_validate(case)
        return self._launch("case", model.model_dump(), self._case_chain)

    async def submit_feedback(self, appointment_id: str, patient_id: str, rating: int, text: str) -> str:
        """Короткая цепочка отзыва: feedback(analyze) → feedback_reply, если отзыв негативный."""
        if not 1 <= rating <= 5:
            raise ValueError("rating должен быть от 1 до 5")
        data = {"appointment_id": appointment_id, "patient_id": patient_id, "rating": rating, "text": text}
        return self._launch("feedback", data, self._feedback_chain)

    async def wait_run(self, run_id: str, timeout: float | None = None) -> RunRecord:
        """Ждёт завершения запуска (для CLI и тестов)."""
        run = self.store.get(run_id)
        if run is None:
            raise KeyError(run_id)
        event = self._done.get(run_id)
        if run.status == "running" and event is not None:
            await asyncio.wait_for(event.wait(), timeout)
        return run

    async def queue_stats(self) -> list[dict[str, Any]]:
        """Состояние очередей JetStream (по консьюмеру `step-*`); пусто, если шина не умеет."""
        getter = getattr(self.bus, "consumer_stats", None)
        if getter is None:
            return []
        return list(await getter())

    # ---- запуск ---------------------------------------------------------------

    def _launch(self, kind: str, case: dict[str, Any], chain: Any) -> str:
        run = RunRecord(run_id=new_id(), kind=kind, case=case, started_at=utc_now_str())
        span = self.tracer.start_span("pipeline.run", attributes={"run.id": run.run_id, "run.kind": kind})
        run.trace_id = format(span.get_span_context().trace_id, "032x")
        self.store.add(run)
        self.metrics.runs_by_status["running"] += 1
        self._done[run.run_id] = asyncio.Event()
        task = asyncio.create_task(self._execute(run, span, chain), name=f"run-{run.run_id}")
        self._tasks[run.run_id] = task
        task.add_done_callback(lambda _t, rid=run.run_id: self._tasks.pop(rid, None))
        return run.run_id

    async def _execute(self, run: RunRecord, span: Span, chain: Any) -> None:
        with trace.use_span(span, end_on_exit=True):
            try:
                await chain(run)
            except asyncio.CancelledError:
                self._finish(run, "failed", error="оркестратор остановлен")
                raise
            except Exception as exc:
                log.exception("необработанная ошибка запуска %s", run.run_id)
                self._finish(run, "failed", error=f"внутренняя ошибка: {exc}")
            finally:
                if run.status == "running":
                    self._finish(run, "failed", error="цепочка завершилась без статуса")
                span.set_attribute("run.status", run.status)
                await self._emit(
                    "stopped" if run.status == "stopped" else ("failed" if run.status == "failed" else "completed"),
                    run,
                    summary=run.recommendation or run.error or "запуск завершён",
                    only_if=run.status != "stopped",  # событие stopped уже отправлено в цепочке
                )
                done = self._done.pop(run.run_id, None)
                if done is not None:
                    done.set()

    def _finish(self, run: RunRecord, status: str, *, error: str = "", recommendation: str = "") -> None:
        if run.status != "running":
            return
        run.status = status
        run.error = error
        run.recommendation = recommendation
        run.finished_at = utc_now_str()
        for rec in run.steps:
            if rec.status == "pending":
                rec.status = "skipped"
        self.metrics.runs_by_status["running"] -= 1
        self.metrics.runs_by_status[status] += 1
        self.store.touch(run)

    # ---- цепочки --------------------------------------------------------------

    async def _case_chain(self, run: RunRecord) -> None:
        for name, kind in CASE_CHAIN:
            run.step(name).kind = kind
        self.store.touch(run)
        history: dict[str, dict[str, Any]] = {}
        previous: dict[str, Any] = {}
        for name, kind in CASE_CHAIN:
            if name == "booking":
                outcome = await self._run_booking(run, history)
            else:
                payload = {"case": run.case, "previous": previous, "history": dict(history)}
                outcome = await self._run_step(run, name, kind, payload)
            if name == "booking" and outcome.error == "no_bids":
                # Нет свободного врача в допустимом окне — это не сбой системы, а исход для пациента:
                # он должен получить рекомендацию, что делать дальше (раздел 6, п. 3).
                advice = NO_SLOTS_URGENT if history.get("triage", {}).get("urgency") == "urgent" else NO_SLOTS_ROUTINE
                await self._emit("stopped", run, step="booking", summary=advice)
                self._finish(run, "stopped", recommendation=advice)
                return
            if not outcome.success:
                self._finish(run, "failed", error=f"{name}: {outcome.error}")
                return
            history[name] = outcome.output
            previous = outcome.output
            if name == "triage" and outcome.output.get("urgency") == "emergency":
                await self._emit("stopped", run, step="triage", summary=EMERGENCY_RECOMMENDATION)
                self._finish(run, "stopped", recommendation=EMERGENCY_RECOMMENDATION)
                return
        self._finish(run, "completed")

    async def _feedback_chain(self, run: RunRecord) -> None:
        run.step("feedback").kind = "analyze"
        run.step("feedback_reply").kind = "feedback_reply"
        self.store.touch(run)
        payload = {"case": run.case, "previous": {}, "history": {}, "feedback": run.case}
        analysis = await self._run_step(run, "feedback", "analyze", payload)
        if not analysis.success:
            self._finish(run, "failed", error=f"feedback: {analysis.error}")
            return
        if analysis.output.get("sentiment") == "negative" or analysis.output.get("escalate") is True:
            payload = {
                "case": run.case,
                "previous": analysis.output,
                "history": {"feedback": analysis.output},
                "feedback": run.case,
            }
            reply = await self._run_step(run, "feedback_reply", "feedback_reply", payload)
            if not reply.success:
                self._finish(run, "failed", error=f"feedback_reply: {reply.error}")
                return
        self._finish(run, "completed")

    # ---- шаг с повторами --------------------------------------------------------

    async def _run_step(self, run: RunRecord, step: str, kind: str, payload: dict[str, Any]) -> StepOutcome:
        rec = run.step(step)
        rec.kind = kind
        rec.status = "running"
        rec.started_at = rec.started_at or utc_now_str()
        timeout = self.settings.step_timeout(step)
        last_error = "нет попыток"
        loop = asyncio.get_running_loop()
        with self.tracer.start_as_current_span(f"step {step}") as span:
            span.set_attribute("step.name", step)
            for attempt in range(1, self.settings.max_attempts + 1):
                # Новый task_id на каждую попытку: запоздалый результат прошлой попытки
                # не совпадёт с ожидаемым и будет проигнорирован (раздел 5).
                task = Task(run_id=run.run_id, step=step, kind=kind, attempt=attempt, payload=payload)
                pending = _Pending(loop.create_future(), run.run_id, step, attempt)
                self._pending[task.task_id] = pending
                rec.attempts = attempt
                self.store.touch(run)
                seq: int | None = None
                try:
                    seq = await self.bus.publish_task(task)
                    self.metrics.tasks += 1
                    await self._emit("published", run, step=step, task=task, summary=f"attempt={attempt}")
                    result = await asyncio.wait_for(pending.future, timeout)
                except TimeoutError:
                    self.metrics.timeouts += 1
                    last_error = f"таймаут {timeout:g} с"
                    await self._emit("timeout", run, step=step, task=task, summary=f"attempt={attempt}")
                    await self._discard(seq)
                    retryable = True
                except Exception as exc:  # сбой публикации в шину — временная ошибка
                    last_error = f"ошибка публикации: {exc}"
                    log.warning("публикация задачи %s не удалась: %s", task.task_id, exc)
                    retryable = True
                else:
                    rec.agent_id = result.agent_id
                    if result.success:
                        rec.status = "completed"
                        rec.output = result.output
                        rec.error = ""
                        rec.finished_at = utc_now_str()
                        self.store.touch(run)
                        return StepOutcome(True, result.output, agent_id=result.agent_id)
                    last_error = result.error or "агент вернул ошибку"
                    retryable = result.retryable
                finally:
                    self._pending.pop(task.task_id, None)
                rec.error = last_error
                if not retryable or attempt >= self.settings.max_attempts:
                    break
                self.metrics.retries += 1
                await self._emit("retry", run, step=step, task=task, summary=f"{last_error}; attempt={attempt + 1}")
            rec.status = "failed"
            rec.finished_at = utc_now_str()
            self.store.touch(run)
            span.set_status(trace.Status(trace.StatusCode.ERROR, last_error))
            return _fail(last_error)

    async def _discard(self, seq: int | None) -> None:
        """Убирает из потока задачу попытки, ответа на которую больше не ждём: иначе агенты
        выполняют работу для проваленных запусков, а backlog обманывает автомасштабировщик."""
        if seq is None:
            return
        try:
            await self.bus.discard_task(seq)
        except Exception:
            log.warning("не удалось удалить устаревшую задачу seq=%s", seq, exc_info=True)

    # ---- booking: аукцион ---------------------------------------------------------

    async def _run_booking(self, run: RunRecord, history: dict[str, dict[str, Any]]) -> StepOutcome:
        triage = history.get("triage", {})
        intake = history.get("intake", {})
        specialty = triage.get("specialty") or intake.get("specialty") or "therapist"
        urgency = triage.get("urgency") or "routine"
        max_wait = base_wait = float(triage.get("max_wait_hours") or 72)
        rec = run.step("booking")
        rec.status = "running"
        rec.started_at = utc_now_str()
        exclude: list[str] = []
        last_error = "нет раундов"
        with self.tracer.start_as_current_span("step booking") as span:
            for round_no in range(1, self.settings.max_auction_rounds + 1):
                rec.attempts = round_no
                self.store.touch(run)
                outcome = await self._auction_round(run, specialty, urgency, max_wait, exclude)
                if outcome.error == "no_bids" and urgency == "routine" and max_wait == base_wait:
                    # Плановому пациенту лучше запись позже, чем никакой: одно расширение окна.
                    max_wait = base_wait * self.settings.no_bids_extend_factor
                    await self._emit("retry", run, step="booking", summary=f"no_bids: окно расширено до {max_wait:g} ч")
                    outcome = await self._auction_round(run, specialty, urgency, max_wait, exclude)
                if outcome.success or outcome.error == "no_bids":
                    if outcome.success:
                        rec.status, rec.output, rec.agent_id = "completed", outcome.output, outcome.agent_id
                    else:
                        rec.status, rec.error = "failed", "no_bids"
                    rec.finished_at = utc_now_str()
                    self.store.touch(run)
                    return outcome
                last_error = outcome.error
                if outcome.error == "book_timeout" and outcome.agent_id:
                    exclude.append(outcome.agent_id)
                self.metrics.retries += 1
                await self._emit("retry", run, step="booking", summary=f"{last_error}; round={round_no + 1}")
            rec.status, rec.error = "failed", last_error
            rec.finished_at = utc_now_str()
            self.store.touch(run)
            span.set_status(trace.Status(trace.StatusCode.ERROR, last_error))
            return _fail(f"аукцион не удался за {self.settings.max_auction_rounds} раунда: {last_error}")

    async def _auction_round(
        self, run: RunRecord, specialty: str, urgency: str, max_wait: float, exclude: list[str]
    ) -> StepOutcome:
        req = AuctionRequest(
            run_id=run.run_id,
            specialty=specialty,
            urgency=urgency,
            max_wait_hours=max_wait,
            exclude_agents=list(exclude),
        )
        self.metrics.auctions += 1
        replies = await self.bus.collect(
            "auction.booking", req.model_dump_json().encode(), self.settings.auction_window_s
        )
        bids = parse_bids([m.data for m in replies], req.auction_id)
        bids = [b for b in bids if b.agent_id not in exclude]
        winner = choose_winner(bids)
        if winner is None:
            return _fail("no_bids")
        await self._emit(
            "won",
            run,
            step="booking",
            summary=f"{winner.agent_id} doctor={winner.doctor_id} cost={winner.cost:.3f} bids={len(bids)}",
        )
        book = BookRequest(
            auction_id=req.auction_id,
            run_id=run.run_id,
            doctor_id=winner.doctor_id,
            slot_start=winner.slot_start,
            patient_id=str(run.case.get("patient_id", "")),
            patient_name=str(run.case.get("patient_name", "")),
            max_wait_hours=max_wait,
        )
        try:
            msg = await self.bus.request(
                f"agent.{winner.agent_id}.book", book.model_dump_json().encode(), self.settings.book_timeout_s
            )
            reply = json.loads(msg.data)
        except TimeoutError:
            return StepOutcome(False, {}, "book_timeout", winner.agent_id)
        except ValueError:
            return StepOutcome(False, {}, "book_bad_reply", winner.agent_id)
        if not isinstance(reply, dict):
            return StepOutcome(False, {}, "book_bad_reply", winner.agent_id)
        ok = reply.get("success", not reply.get("error"))
        if not ok:
            return StepOutcome(False, {}, str(reply.get("error") or "book_failed"), winner.agent_id)
        inner = reply.get("output")
        if isinstance(inner, dict):
            output = dict(inner)
        else:
            output = {k: v for k, v in reply.items() if k not in ("success", "error")}
        output.setdefault("agent_id", winner.agent_id)
        # Итоги аукциона знает только оркестратор: агент записи кладёт сюда нули.
        output["auction"] = {"bids": len(bids), "winner_cost": winner.cost}
        return StepOutcome(True, output, agent_id=winner.agent_id)

    # ---- обработчики шины -----------------------------------------------------------

    async def _on_result(self, msg: Any) -> None:
        try:
            result = Result.model_validate_json(msg.data)
        except ValueError:
            log.warning("некорректный результат в %s", msg.subject)
            return
        pending = self._pending.get(result.task_id)
        if pending is None or pending.run_id != result.run_id or pending.step != result.step:
            log.info("устаревший или чужой результат проигнорирован: task_id=%s", result.task_id)
            return
        if not pending.future.done():
            pending.future.set_result(result)

    async def _on_heartbeat(self, msg: Any) -> None:
        try:
            self.registry.update(Heartbeat.model_validate_json(msg.data))
        except ValueError:
            log.warning("некорректный heartbeat")

    async def _emit(
        self,
        action: str,
        run: RunRecord,
        *,
        step: str = "",
        task: Task | None = None,
        summary: str = "",
        only_if: bool = True,
    ) -> None:
        """Событие журнала; сбой публикации не должен ломать запуск."""
        if not only_if:
            return
        trace_id, span_id = current_ids()
        event = Event(
            source=SOURCE,
            action=action,
            run_id=run.run_id,
            task_id=task.task_id if task else "",
            step=step,
            trace_id=trace_id,
            span_id=span_id,
            subject=f"tasks.{step}" if task else "",
            summary=summary,
        )
        try:
            await publish_event(self.bus, event)
        except Exception:
            log.warning("не удалось опубликовать событие %s", action, exc_info=True)


__all__ = ["CASE_CHAIN", "Orchestrator", "StepOutcome"]

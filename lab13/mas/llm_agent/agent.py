"""LLM-агент: читает задачи своего шага из JetStream и отвечает по контракту."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from opentelemetry.trace import Tracer

from mas.common.bus import MessageBus, TaskMessage, publish_event, publish_heartbeat, publish_result
from mas.common.models import Event, Heartbeat, Result, Task, utc_now_str
from mas.common.roles import Role
from mas.common.settings import Settings
from mas.common.telemetry import current_ids, extract_context
from mas.llm_agent.fallback import fallback_classify, fallback_reply
from mas.llm_agent.ollama import LLMError, OllamaClient, build_chat_payload
from mas.llm_agent.prompts import (
    build_intake_messages,
    build_reply_messages,
    parse_intake_response,
    parse_reply_response,
)

log = logging.getLogger("mas.llm_agent")

HEARTBEAT_INTERVAL_S = 5.0
SHUTDOWN_GRACE_S = 10.0
SUPPORTED_STEPS = ("intake", "feedback_reply")


class LLMAgent:
    def __init__(
        self,
        bus: MessageBus,
        role: Role,
        settings: Settings,
        ollama: OllamaClient,
        tracer: Tracer,
        agent_id: str | None = None,
    ) -> None:
        if role.step not in SUPPORTED_STEPS:
            raise ValueError(f"LLM-агент не поддерживает шаг {role.step!r}")
        self.bus = bus
        self.role = role
        self.settings = settings
        self.ollama = ollama
        self.tracer = tracer
        self.agent_id = agent_id or settings.agent_id or f"llm-{role.step}-{os.getpid()}"
        self.processed = 0
        self.failed = 0
        self.inflight = 0
        self.started_at = utc_now_str()
        self._stopping = asyncio.Event()
        self._handlers: set[asyncio.Task[None]] = set()

    # ---- обработка задачи -------------------------------------------------------

    async def compute(self, task: Task) -> tuple[dict[str, Any], str]:
        """Возвращает (output, причина_fallback); причина пустая, если ответила модель."""
        if task.step == "intake":
            return await self._intake(task)
        if task.step == "feedback_reply":
            return await self._feedback_reply(task)
        raise ValueError(f"неизвестный шаг {task.step!r}")

    def _model_options(self) -> dict[str, Any]:
        p = self.role.params
        return {
            "temperature": float(p.get("temperature", 0.2)),
            "num_predict": int(p.get("num_predict", 512)),
            "repeat_penalty": float(p.get("repeat_penalty", 1.1)),
        }

    async def _ask_model(self, messages: list[dict[str, str]], *, json_format: bool) -> str:
        model = str(self.role.params.get("model") or self.settings.ollama_model)
        payload = build_chat_payload(model, messages, json_format=json_format, **self._model_options())
        return await self.ollama.chat(payload)

    async def _intake(self, task: Task) -> tuple[dict[str, Any], str]:
        case = task.payload.get("case") or {}
        complaint = str(case.get("complaint", ""))
        if not complaint.strip():
            raise ValueError("в payload.case нет complaint")
        preferred = str(case.get("preferred_specialty") or "")
        age = case.get("age") if isinstance(case.get("age"), int) else None
        if self.settings.llm_mode == "fallback":
            return fallback_classify(complaint, preferred).model_dump(), "LLM_MODE=fallback"
        try:
            content = await self._ask_model(build_intake_messages(self.role, complaint, age), json_format=True)
            return parse_intake_response(content, complaint).model_dump(), ""
        except LLMError as exc:
            return fallback_classify(complaint, preferred).model_dump(), str(exc)

    async def _feedback_reply(self, task: Task) -> tuple[dict[str, Any], str]:
        case = task.payload.get("case") or {}
        previous = task.payload.get("previous") or {}
        text = str(case.get("text", ""))
        rating = case.get("rating") if isinstance(case.get("rating"), int) else None
        topics = [str(t) for t in previous.get("topics", []) if isinstance(t, str)]
        if self.settings.llm_mode == "fallback":
            return fallback_reply(rating, topics).model_dump(), "LLM_MODE=fallback"
        try:
            content = await self._ask_model(build_reply_messages(self.role, text, rating, topics), json_format=False)
            return parse_reply_response(content).model_dump(), ""
        except LLMError as exc:
            return fallback_reply(rating, topics).model_dump(), str(exc)

    async def _handle(self, msg: TaskMessage) -> None:
        self.inflight += 1
        try:
            try:
                task = Task.model_validate_json(msg.data)
            except ValueError:
                log.error("некорректная задача, сообщение отклонено")
                self.failed += 1
                await msg.term()
                return
            with self.tracer.start_as_current_span(
                f"process {task.step}", context=extract_context(msg.headers)
            ) as span:
                span.set_attribute("run.id", task.run_id)
                await self._event("received", task, f"kind={task.kind} attempt={task.attempt}")
                started = time.monotonic()
                try:
                    output, reason = await self.compute(task)
                    result = Result(
                        task_id=task.task_id,
                        run_id=task.run_id,
                        step=task.step,
                        agent_id=self.agent_id,
                        success=True,
                        output=output,
                        duration_ms=int((time.monotonic() - started) * 1000),
                    )
                    summary = f"source={output.get('source', '')}" + (f" reason={reason}" if reason else "")
                    action = "completed"
                except Exception as exc:
                    log.exception("задача %s не обработана", task.task_id)
                    result = Result(
                        task_id=task.task_id,
                        run_id=task.run_id,
                        step=task.step,
                        agent_id=self.agent_id,
                        success=False,
                        error=str(exc),
                        retryable=not isinstance(exc, ValueError),
                        duration_ms=int((time.monotonic() - started) * 1000),
                    )
                    summary, action = str(exc), "failed"
                    self.failed += 1
                await self._event(action, task, summary)
                try:
                    await publish_result(self.bus, result)
                except Exception:
                    log.exception("результат не опубликован, задача возвращена в очередь")
                    await msg.nak()
                    return
                if result.success:
                    self.processed += 1
                await msg.ack()
        finally:
            self.inflight -= 1

    async def _event(self, action: str, task: Task, summary: str) -> None:
        trace_id, span_id = current_ids()
        event = Event(
            source=self.agent_id,
            action=action,
            run_id=task.run_id,
            task_id=task.task_id,
            step=task.step,
            trace_id=trace_id,
            span_id=span_id,
            subject=f"tasks.{task.step}",
            summary=summary,
        )
        try:
            await publish_event(self.bus, event)
        except Exception:
            log.warning("событие %s не опубликовано", action, exc_info=True)

    # ---- жизненный цикл ----------------------------------------------------------

    def heartbeat(self, status: str = "ready") -> Heartbeat:
        return Heartbeat(
            agent_id=self.agent_id,
            role=self.role.step,
            kind=self.role.kind,
            title=self.role.title,
            status=status,
            processed=self.processed,
            failed=self.failed,
            inflight=self.inflight,
            started_at=self.started_at,
            host=f"pid-{os.getpid()}",
            lang="python",
        )

    async def _heartbeat_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                await publish_heartbeat(self.bus, self.heartbeat("busy" if self.inflight else "ready"))
            except Exception:
                log.warning("heartbeat не отправлен", exc_info=True)
            try:
                await asyncio.wait_for(self._stopping.wait(), HEARTBEAT_INTERVAL_S)
            except TimeoutError:
                pass

    def request_stop(self) -> None:
        self._stopping.set()

    async def run(self) -> None:
        """Основной цикл: до `request_stop()` берёт задачи, затем дорабатывает текущие (до 10 с)."""
        concurrency = max(1, int(self.role.params.get("concurrency", 2)))
        beat = asyncio.create_task(self._heartbeat_loop())
        try:
            while not self._stopping.is_set():
                free = concurrency - self.inflight
                if free <= 0:
                    await asyncio.sleep(0.05)
                    continue
                try:
                    messages = await self.bus.fetch_tasks(self.role.step, free, 1.0)
                except Exception:
                    log.warning("ошибка выборки задач", exc_info=True)
                    await asyncio.sleep(1.0)
                    continue
                for msg in messages:
                    self.inflight += 1  # резервируем место сразу, до старта обработчика
                    handler = asyncio.create_task(self._guarded(msg))
                    self._handlers.add(handler)
                    handler.add_done_callback(self._handlers.discard)
        finally:
            if self._handlers:
                await asyncio.wait(self._handlers, timeout=SHUTDOWN_GRACE_S)
            self._stopping.set()
            await beat
            try:
                await publish_heartbeat(self.bus, self.heartbeat("stopping"))
            except Exception:
                log.warning("heartbeat stopping не отправлен", exc_info=True)

    async def _guarded(self, msg: TaskMessage) -> None:
        self.inflight -= 1  # _handle сам увеличит счётчик; снимаем резерв
        await self._handle(msg)

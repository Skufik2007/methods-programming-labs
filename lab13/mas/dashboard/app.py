"""Фабрика приложения дашборда: FastAPI + Jinja2, в одном процессе с оркестратором.

Оркестратор создаётся и подключается в lifespan (не при импорте). Если передан готовый
(например, фейковый в тестах), приложение его не запускает и не останавливает, а только
читает `store`, `registry`, `metrics` и вызывает `start_case`/`submit_feedback`.
Подписка дашборда на `events.>` и `notify.patient` делается в lifespan; состояние
приложения (`app.state`) инициализируется в `create_app`, поэтому ASGI-тесты через
`httpx.ASGITransport` (без lifespan) тоже работают.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from mas.common.bus import BusMessage, NatsBus, Subscription
from mas.common.models import Case
from mas.common.specialties import SPECIALTIES
from mas.dashboard.events import NOTIFY_SUBJECT, EventStore
from mas.dashboard.security import SecurityHeadersMiddleware
from mas.dashboard.settings import DashboardSettings, get_dashboard_settings
from mas.dashboard.views import (
    ACTION_LABELS,
    RUN_STATUS,
    SPECIALTY_LABELS,
    agent_row,
    autoscaler_row,
    error_detail,
    event_row,
    metric_rows,
    queue_row,
    run_detail,
    run_row,
    step_row,
)
from mas.orchestrator.orchestrator import Orchestrator
from mas.orchestrator.store import RunRecord

log = logging.getLogger("mas.dashboard")

BASE_DIR = Path(__file__).resolve().parent
MAX_FORM_BYTES = 32 * 1024
MAX_LOAD = 50
STORE_SCAN_LIMIT = 100_000  # InMemoryRunStore хранит не больше 500 запусков; берём «всё»

RunStatusFilter = Literal["", "running", "completed", "stopped", "failed"]
Fetcher = Callable[[str, float], Awaitable[dict[str, Any]]]


# ---- модели входных данных (общие для JSON API и HTML-форм) -----------------------------------


class CaseIn(Case):
    """Обращение из формы/API: `patient_id` необязателен — при пустом значении генерируется."""

    patient_id: str = Field(default="", max_length=100)


class FeedbackIn(BaseModel):
    appointment_id: str = Field(min_length=1, max_length=100)
    patient_id: str = Field(min_length=1, max_length=100)
    rating: int = Field(ge=1, le=5)
    text: str = Field(default="", max_length=2000)


class LoadIn(BaseModel):
    count: int = Field(ge=1, le=MAX_LOAD)


# ---- вспомогательное -----------------------------------------------------------------------------


async def fetch_autoscaler(url: str, timeout: float) -> dict[str, Any]:
    """`GET <AUTOSCALER_URL>/status`. Недоступность — не ошибка страницы, а `available=False`."""
    if not url:
        return {"available": False, "steps": [], "message": "автомасштабировщик не настроен (AUTOSCALER_URL)"}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url.rstrip("/") + "/status")
            response.raise_for_status()
            data = response.json()
        steps = data.get("steps") if isinstance(data, dict) else None
        if not isinstance(steps, list):
            raise ValueError("нет поля steps")
        rows = [autoscaler_row(s) for s in steps if isinstance(s, dict)]
    except (httpx.HTTPError, ValueError) as exc:
        log.info("автомасштабировщик недоступен: %s", exc)
        return {"available": False, "steps": [], "message": "автомасштабировщик недоступен"}
    return {"available": True, "steps": rows, "message": ""}


def new_patient_id() -> str:
    return "web-" + secrets.token_hex(4)


def _page(items: list[Any], limit: int, offset: int) -> list[Any]:
    return items[offset : offset + limit]


def _filter_runs(orch: Any, status: str) -> list[RunRecord]:
    runs = orch.store.list(STORE_SCAN_LIMIT)
    return [r for r in runs if not status or r.status == status]


def _pager(path: str, params: dict[str, Any], total: int, limit: int, offset: int) -> dict[str, Any]:
    """Данные для ссылок «назад/вперёд»; `limit`/`offset` одинаково везде (страницы и API)."""

    def link(new_offset: int) -> str:
        query = {k: v for k, v in params.items() if v not in ("", None)}
        query.update({"limit": limit, "offset": new_offset})
        return f"{path}?{urlencode(query)}"

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "first": min(offset + 1, total) if total else 0,
        "last": min(offset + limit, total),
        "prev": link(max(offset - limit, 0)) if offset > 0 else "",
        "next": link(offset + limit) if offset + limit < total else "",
    }


def _form_orchestrator(request: Request) -> Any:
    orch = request.app.state.orchestrator
    if orch is None:
        raise StarletteHTTPException(503, "Оркестратор ещё не запущен")
    return orch


def _check_origin(request: Request) -> None:
    """Простая защита от CSRF: запрос с чужого Origin отклоняется (браузеры шлют Origin при POST)."""
    origin = request.headers.get("origin")
    if origin is not None and urlparse(origin).netloc != request.headers.get("host", ""):
        raise StarletteHTTPException(403, "Запрос с чужого origin отклонён")


async def _read_form(request: Request) -> dict[str, str]:
    """application/x-www-form-urlencoded без python-multipart (формы дашборда — только urlencoded)."""
    body = await request.body()
    if len(body) > MAX_FORM_BYTES:
        raise StarletteHTTPException(413, "Слишком большой запрос")
    parsed = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    return {key: values[-1] for key, values in parsed.items()}


# ---- приложение --------------------------------------------------------------------------------------


def create_app(settings: DashboardSettings | None = None, orchestrator: Any = None) -> FastAPI:
    """Собирает приложение. Подключений при вызове не создаёт."""
    cfg = settings or get_dashboard_settings()
    templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned_bus: NatsBus | None = None
        owned_orch: Orchestrator | None = None
        subs: list[Subscription] = []
        try:
            if app.state.orchestrator is None:
                owned_bus = NatsBus(cfg.nats_url, name="dashboard")
                await owned_bus.connect()
                await owned_bus.ensure_stream()
                owned_orch = Orchestrator(owned_bus, cfg)
                await owned_orch.start()
                app.state.orchestrator = owned_orch
            bus = getattr(app.state.orchestrator, "bus", None)
            if bus is not None:
                events: EventStore = app.state.events

                async def on_message(msg: BusMessage) -> None:
                    events.add_message(msg.subject, msg.data)

                subs.append(await bus.subscribe("events.>", on_message))
                subs.append(await bus.subscribe(NOTIFY_SUBJECT, on_message))
            yield
        finally:
            for sub in subs:
                try:
                    await sub.unsubscribe()
                except Exception:
                    log.warning("не удалось отписаться", exc_info=True)
            if owned_orch is not None:
                await owned_orch.stop()
                app.state.orchestrator = None
            if owned_bus is not None:
                await owned_bus.close()

    # Документация OpenAPI тянет скрипты с CDN и inline — несовместима со строгой CSP, отключена.
    app = FastAPI(title="Панель мониторинга MAS", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = cfg
    app.state.orchestrator = orchestrator
    app.state.events = EventStore(cfg.max_events)
    app.state.autoscaler_fetch = fetch_autoscaler  # подменяется в тестах
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
    app.add_middleware(SecurityHeadersMiddleware)

    def render(request: Request, name: str, context: dict[str, Any], status_code: int = 200) -> Response:
        context = {
            "jaeger_url": cfg.jaeger_ui_url.rstrip("/"),
            "specialties": [(code, SPECIALTY_LABELS.get(code, code)) for code in SPECIALTIES],
            **context,
        }
        return templates.TemplateResponse(request, name, context, status_code=status_code)

    def events_store() -> EventStore:
        return app.state.events

    def orch_of(request: Request) -> Any:
        return _form_orchestrator(request)

    async def collect_queues(orch: Any) -> dict[str, Any]:
        getter = getattr(orch, "queue_stats", None)
        if getter is None:
            return {"available": False, "queues": [], "message": "статистика очередей недоступна"}
        try:
            raw = await asyncio.wait_for(getter(), cfg.probe_timeout_s)
        except Exception as exc:
            log.info("очереди JetStream недоступны: %s", exc)
            return {"available": False, "queues": [], "message": "очереди JetStream недоступны"}
        return {"available": True, "queues": [queue_row(r) for r in raw], "message": ""}

    async def collect_autoscaler() -> dict[str, Any]:
        try:
            return await app.state.autoscaler_fetch(cfg.autoscaler_url, cfg.probe_timeout_s)
        except Exception:
            log.warning("сбой опроса автомасштабировщика", exc_info=True)
            return {"available": False, "steps": [], "message": "автомасштабировщик недоступен"}

    def agents_data(orch: Any) -> list[dict[str, Any]]:
        return [agent_row(r) for r in orch.registry.snapshot()]

    def runs_data(orch: Any, status: str, limit: int, offset: int) -> dict[str, Any]:
        runs = _filter_runs(orch, status)
        return {
            "items": [run_row(r) for r in _page(runs, limit, offset)],
            "total": len(runs),
            "limit": limit,
            "offset": offset,
        }

    def events_data(source: str, action: str, limit: int, offset: int) -> dict[str, Any]:
        events = events_store().query(source=source, action=action)
        return {
            "items": [event_row(e) for e in _page(events, limit, offset)],
            "total": len(events),
            "limit": limit,
            "offset": offset,
        }

    def run_data(orch: Any, run_id: str) -> dict[str, Any] | None:
        run = orch.store.get(run_id)
        if run is None:
            return None
        return {
            "run": run_detail(run),
            "steps": [step_row(s) for s in run.steps],
            "events": [event_row(e) for e in events_store().for_run(run_id)],
        }

    # ---- обработчики ошибок -------------------------------------------------------------------------

    @app.exception_handler(RequestValidationError)
    async def on_validation_error(request: Request, exc: RequestValidationError) -> Response:
        body = error_detail(list(exc.errors()))
        if request.url.path.startswith("/api/"):
            return JSONResponse(body, status_code=422)
        return render(request, "error.html", {"title": "Некорректный запрос", "message": body["detail"]}, 422)

    @app.exception_handler(StarletteHTTPException)
    async def on_http_error(request: Request, exc: StarletteHTTPException) -> Response:
        messages = {404: "Не найдено", 405: "Метод не поддерживается"}
        message = messages.get(exc.status_code) or str(exc.detail)
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": message}, status_code=exc.status_code)
        context = {"title": f"Ошибка {exc.status_code}", "message": message}
        return render(request, "error.html", context, exc.status_code)

    # ---- страницы --------------------------------------------------------------------------------------

    @app.get("/", include_in_schema=False)
    async def overview(request: Request) -> Response:
        orch = orch_of(request)
        queues, autoscaler = await asyncio.gather(collect_queues(orch), collect_autoscaler())
        context = {
            "page": "overview",
            "agents": agents_data(orch),
            "queues": queues,
            "metrics": metric_rows(orch.metrics.to_dict()),
            "runs": runs_data(orch, "", 10, 0)["items"],
            "autoscaler": autoscaler,
        }
        return render(request, "overview.html", context)

    @app.get("/runs", include_in_schema=False)
    async def runs_page(
        request: Request,
        status: RunStatusFilter = "",
        limit: int = Query(20, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ) -> Response:
        data = runs_data(orch_of(request), status, limit, offset)
        context = {
            "page": "runs",
            "runs": data["items"],
            "status": status,
            "statuses": [(code, label) for code, (label, _css) in RUN_STATUS.items()],
            "pager": _pager("/runs", {"status": status}, data["total"], limit, offset),
            "live_query": urlencode({"status": status, "limit": limit, "offset": offset}),
        }
        return render(request, "runs.html", context)

    @app.get("/runs/{run_id}", include_in_schema=False)
    async def run_page(request: Request, run_id: str) -> Response:
        data = run_data(orch_of(request), run_id)
        if data is None:
            return render(request, "error.html", {"title": "Запуск не найден", "message": run_id[:100]}, 404)
        return render(request, "run.html", {"page": "runs", **data})

    @app.get("/events", include_in_schema=False)
    async def events_page(
        request: Request,
        source: str = Query("", max_length=100),
        action: str = Query("", max_length=50),
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ) -> Response:
        data = events_data(source, action, limit, offset)
        context = {
            "page": "events",
            "events": data["items"],
            "source": source,
            "action": action,
            "sources": events_store().sources(),
            "actions": list(ACTION_LABELS.items()),
            "notifications": [event_row(e) for e in events_store().notifications()[:20]],
            "pager": _pager("/events", {"source": source, "action": action}, data["total"], limit, offset),
            "live_query": urlencode({"source": source, "action": action, "limit": limit, "offset": offset}),
        }
        return render(request, "events.html", context)

    def new_context(**extra: Any) -> dict[str, Any]:
        base: dict[str, Any] = {
            "page": "new",
            "case_values": {},
            "case_errors": [],
            "feedback_values": {},
            "feedback_errors": [],
            "load_errors": [],
            "max_load": MAX_LOAD,
        }
        return {**base, **extra}

    @app.get("/new", include_in_schema=False)
    async def new_page(request: Request) -> Response:
        return render(request, "new.html", new_context())

    @app.post("/new/case", include_in_schema=False)
    async def new_case_form(request: Request) -> Response:
        _check_origin(request)
        orch = orch_of(request)
        values = await _read_form(request)
        try:
            case = CaseIn.model_validate(values)
        except ValidationError as exc:
            errors = error_detail(list(exc.errors()))["errors"]
            return render(request, "new.html", new_context(case_values=values, case_errors=errors), 422)
        run_id = await orch.start_case({**case.model_dump(), "patient_id": case.patient_id or new_patient_id()})
        return RedirectResponse(f"/runs/{run_id}", status_code=303)

    @app.post("/new/feedback", include_in_schema=False)
    async def new_feedback_form(request: Request) -> Response:
        _check_origin(request)
        orch = orch_of(request)
        values = await _read_form(request)
        try:
            fb = FeedbackIn.model_validate(values)
        except ValidationError as exc:
            errors = error_detail(list(exc.errors()))["errors"]
            return render(request, "new.html", new_context(feedback_values=values, feedback_errors=errors), 422)
        run_id = await orch.submit_feedback(fb.appointment_id, fb.patient_id, fb.rating, fb.text)
        return RedirectResponse(f"/runs/{run_id}", status_code=303)

    @app.post("/new/load", include_in_schema=False)
    async def new_load_form(request: Request) -> Response:
        _check_origin(request)
        orch = orch_of(request)
        values = await _read_form(request)
        try:
            load = LoadIn.model_validate(values)
        except ValidationError as exc:
            errors = error_detail(list(exc.errors()))["errors"]
            return render(request, "new.html", new_context(load_errors=errors), 422)
        await start_load(orch, load.count)
        return RedirectResponse("/runs", status_code=303)

    async def start_load(orch: Any, count: int) -> list[str]:
        # Импорт здесь: демо-наборы нужны только для нагрузки и не должны тянуться при старте.
        from mas.orchestrator.demo import make_case

        base = secrets.randbelow(1000) + 10  # смещаем индекс, чтобы не повторять одни и те же p-001...
        return [await orch.start_case(make_case(base + i)) for i in range(count)]

    # ---- JSON API ---------------------------------------------------------------------------------------

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"status": "ok", "orchestrator": app.state.orchestrator is not None, "events": len(events_store())}

    @app.get("/api/agents")
    async def api_agents(request: Request) -> dict[str, Any]:
        return {"agents": agents_data(orch_of(request))}

    @app.get("/api/queues")
    async def api_queues(request: Request) -> dict[str, Any]:
        return await collect_queues(orch_of(request))

    @app.get("/api/autoscaler")
    async def api_autoscaler() -> dict[str, Any]:
        return await collect_autoscaler()

    @app.get("/api/metrics")
    async def api_metrics(request: Request) -> dict[str, Any]:
        return {"metrics": metric_rows(orch_of(request).metrics.to_dict())}

    @app.get("/api/runs")
    async def api_runs(
        request: Request,
        status: RunStatusFilter = "",
        limit: int = Query(20, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        return runs_data(orch_of(request), status, limit, offset)

    @app.get("/api/runs/{run_id}")
    async def api_run(request: Request, run_id: str) -> Response:
        data = run_data(orch_of(request), run_id)
        if data is None:
            return JSONResponse({"detail": "Запуск не найден"}, status_code=404)
        return JSONResponse(data)

    @app.get("/api/events")
    async def api_events(
        source: str = Query("", max_length=100),
        action: str = Query("", max_length=50),
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        return events_data(source, action, limit, offset)

    @app.get("/api/notifications")
    async def api_notifications(limit: int = Query(20, ge=1, le=200), offset: int = Query(0, ge=0)) -> dict[str, Any]:
        items = events_store().notifications()
        return {
            "items": [event_row(e) for e in _page(items, limit, offset)],
            "total": len(items),
            "limit": limit,
            "offset": offset,
        }

    @app.post("/api/cases", status_code=202)
    async def api_create_case(request: Request, case: CaseIn) -> dict[str, Any]:
        _check_origin(request)
        orch = orch_of(request)
        run_id = await orch.start_case({**case.model_dump(), "patient_id": case.patient_id or new_patient_id()})
        return {"run_id": run_id, "url": f"/runs/{run_id}"}

    @app.post("/api/feedback", status_code=202)
    async def api_create_feedback(request: Request, fb: FeedbackIn) -> dict[str, Any]:
        _check_origin(request)
        orch = orch_of(request)
        run_id = await orch.submit_feedback(fb.appointment_id, fb.patient_id, fb.rating, fb.text)
        return {"run_id": run_id, "url": f"/runs/{run_id}"}

    @app.post("/api/load", status_code=202)
    async def api_load(request: Request, load: LoadIn) -> dict[str, Any]:
        _check_origin(request)
        run_ids = await start_load(orch_of(request), load.count)
        return {"run_ids": run_ids, "count": len(run_ids)}

    return app


__all__ = ["CaseIn", "FeedbackIn", "create_app", "fetch_autoscaler"]

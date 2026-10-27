"""Дашборд через httpx.ASGITransport с фейковым оркестратором (раздел 7 задания и раздел 13 контракта)."""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from mas.common.models import Event, Heartbeat, new_id, utc_now_str
from mas.dashboard.app import create_app
from mas.dashboard.events import NOTIFY_SUBJECT, EventStore
from mas.dashboard.settings import DashboardSettings
from mas.orchestrator.store import AgentRegistry, InMemoryRunStore, Metrics, RunRecord

STATIC_JS = Path(__file__).resolve().parents[2] / "mas" / "dashboard" / "static" / "app.js"
CASE = {
    "patient_name": "Иванова Анна Петровна",
    "age": 34,
    "phone": "+7 900 000-00-01",
    "complaint": "Третий день температура 38, кашель, болит горло",
    "preferred_specialty": "",
}


class FakeOrchestrator:
    """Минимальный оркестратор: настоящие хранилища, но без шины и цепочек."""

    def __init__(self, queues: list[dict[str, Any]] | None = None) -> None:
        self.store = InMemoryRunStore()
        self.registry = AgentRegistry()
        self.metrics = Metrics()
        self._queues = queues or []

    def make_run(self, kind: str, case: dict[str, Any], status: str = "running") -> str:
        run = RunRecord(run_id=new_id(), kind=kind, case=case, started_at=utc_now_str(), status=status)
        self.store.add(run)
        return run.run_id

    async def start_case(self, case: dict[str, Any]) -> str:
        return self.make_run("case", case)

    async def submit_feedback(self, appointment_id: str, patient_id: str, rating: int, text: str) -> str:
        return self.make_run("feedback", {"appointment_id": appointment_id, "patient_id": patient_id,
                                          "rating": rating, "text": text})

    async def queue_stats(self) -> list[dict[str, Any]]:
        return list(self._queues)


@pytest.fixture
def orch() -> FakeOrchestrator:
    return FakeOrchestrator(queues=[{"step": "triage", "consumer": "step-triage", "pending": 4,
                                     "ack_pending": 1, "redelivered": 0}])


@pytest.fixture
def app(orch: FakeOrchestrator):  # type: ignore[no-untyped-def]
    settings = DashboardSettings(autoscaler_url="", probe_timeout_s=1.0, max_events=50)
    return create_app(settings, orchestrator=orch)


@pytest.fixture
async def client(app) -> AsyncIterator[httpx.AsyncClient]:  # type: ignore[no-untyped-def]
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


# ---- страницы --------------------------------------------------------------------------------------------


async def test_overview_page_renders_key_blocks(client: httpx.AsyncClient) -> None:
    response = await client.get("/")
    assert response.status_code == 200
    html = response.text
    for marker in ("Обзор системы", "Агенты", "Очереди JetStream", "Метрики оркестратора",
                   "Автомасштабировщик", "agents-table", "queues-table", "runs-table"):
        assert marker in html


async def test_runs_page_renders_filters_and_table(client: httpx.AsyncClient) -> None:
    response = await client.get("/runs")
    assert response.status_code == 200
    assert "<h1>Запуски</h1>" in response.text
    assert 'name="status"' in response.text and "runs-table" in response.text


async def test_events_page_renders_journal(client: httpx.AsyncClient) -> None:
    response = await client.get("/events")
    assert response.status_code == 200
    assert "Журнал событий" in response.text


async def test_new_run_page_renders_forms(client: httpx.AsyncClient) -> None:
    response = await client.get("/new")
    assert response.status_code == 200
    html = response.text
    assert "Ручной запуск" in html
    assert 'action="/new/case"' in html and 'name="patient_name"' in html
    assert 'action="/new/feedback"' in html
    assert 'action="/new/load"' in html


async def test_run_page_shows_run_and_unknown_run_is_404(client: httpx.AsyncClient, orch: FakeOrchestrator) -> None:
    run_id = await orch.start_case(CASE)
    response = await client.get(f"/runs/{run_id}")
    assert response.status_code == 200
    assert run_id[:8] in response.text
    missing = await client.get("/runs/нет-такого")
    assert missing.status_code == 404
    assert "Запуск не найден" in missing.text


async def test_healthz_reports_orchestrator(client: httpx.AsyncClient) -> None:
    body = (await client.get("/healthz")).json()
    assert body["status"] == "ok"
    assert body["orchestrator"] is True


# ---- заголовки безопасности (CSP) --------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/runs", "/new", "/api/agents", "/no-such-page"])
async def test_security_headers_on_every_response(client: httpx.AsyncClient, path: str) -> None:
    response = await client.get(path)
    csp = response.headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp
    assert "script-src 'self'" in csp
    assert "unsafe-inline" not in csp
    assert "unsafe-eval" not in csp
    assert "frame-ancestors 'none'" in csp
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"


async def test_pages_have_no_inline_scripts_or_styles(client: httpx.AsyncClient) -> None:
    for path in ("/", "/runs", "/events", "/new"):
        html = (await client.get(path)).text
        assert not re.search(r"<script(?![^>]*\bsrc=)", html), path
        assert "style=" not in html, path


# ---- JSON API, пагинация -----------------------------------------------------------------------------------


async def test_runs_api_paginates_newest_first(client: httpx.AsyncClient, orch: FakeOrchestrator) -> None:
    ids = [await orch.start_case({**CASE, "patient_name": f"Пациент {i}"}) for i in range(5)]
    body = (await client.get("/api/runs", params={"limit": 2, "offset": 1})).json()
    assert body["total"] == 5
    assert body["limit"] == 2
    assert body["offset"] == 1
    assert [item["run_id"] for item in body["items"]] == [ids[3], ids[2]]


async def test_runs_api_filters_by_status(client: httpx.AsyncClient, orch: FakeOrchestrator) -> None:
    orch.make_run("case", CASE, status="completed")
    orch.make_run("case", CASE, status="failed")
    body = (await client.get("/api/runs", params={"status": "completed"})).json()
    assert body["total"] == 1
    assert body["items"][0]["status"] == "completed"


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 201}, {"offset": -1}, {"status": "bogus"}])
async def test_runs_api_rejects_bad_paging_and_filters(client: httpx.AsyncClient, params: dict[str, Any]) -> None:
    assert (await client.get("/api/runs", params=params)).status_code == 422


async def test_run_detail_api_and_404(client: httpx.AsyncClient, orch: FakeOrchestrator) -> None:
    run_id = await orch.start_case(CASE)
    body = (await client.get(f"/api/runs/{run_id}")).json()
    assert body["run"]["run_id"] == run_id
    assert body["run"]["patient"] == CASE["patient_name"]
    assert "steps" in body and "events" in body
    missing = await client.get("/api/runs/нет-такого")
    assert missing.status_code == 404
    assert missing.json() == {"detail": "Запуск не найден"}


async def test_agents_api_lists_heartbeat_agents(client: httpx.AsyncClient, orch: FakeOrchestrator) -> None:
    orch.registry.update(Heartbeat(agent_id="triage-1", role="triage", kind="triage", title="Триаж",
                                   status="ready", lang="go"))
    agents = (await client.get("/api/agents")).json()["agents"]
    assert len(agents) == 1
    assert agents[0]["agent_id"] == "triage-1"
    assert agents[0]["status_label"] == "в сети"


async def test_queues_api_uses_russian_step_labels(client: httpx.AsyncClient) -> None:
    body = (await client.get("/api/queues")).json()
    assert body["available"] is True
    assert body["queues"][0]["step_label"] == "Триаж"
    assert body["queues"][0]["pending"] == 4


async def test_metrics_api_lists_counters(client: httpx.AsyncClient, orch: FakeOrchestrator) -> None:
    orch.metrics.tasks = 7
    rows = (await client.get("/api/metrics")).json()["metrics"]
    assert {"name": "Задач отправлено", "value": 7} in rows


async def test_autoscaler_not_configured_is_not_an_error(client: httpx.AsyncClient) -> None:
    body = (await client.get("/api/autoscaler")).json()
    assert body["available"] is False
    assert "AUTOSCALER_URL" in body["message"]


async def test_unreachable_autoscaler_is_reported_as_unavailable() -> None:
    from mas.dashboard.app import fetch_autoscaler

    result = await fetch_autoscaler("http://127.0.0.1:9", 0.5)
    assert result == {"available": False, "steps": [], "message": "автомасштабировщик недоступен"}


# ---- создание обращения: валидация, Origin, формы ------------------------------------------------------------


async def test_valid_case_is_accepted_and_gets_generated_patient_id(client: httpx.AsyncClient,
                                                                    orch: FakeOrchestrator) -> None:
    response = await client.post("/api/cases", json=CASE)
    assert response.status_code == 202
    body = response.json()
    assert body["url"] == f"/runs/{body['run_id']}"
    run = orch.store.get(body["run_id"])
    assert run is not None
    assert run.case["patient_id"].startswith("web-")


async def test_invalid_case_gets_422_with_russian_message(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/cases", json={"patient_name": "", "age": 200, "complaint": "ab"})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "ФИО" in detail and "Возраст" in detail and "Жалоба" in detail
    assert "минимум 3" in detail
    assert "значение не больше 120" in detail
    assert "обязательное поле" in detail


async def test_case_with_non_numeric_age_is_422(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/cases", json={**CASE, "age": "тридцать"})
    assert response.status_code == 422
    assert "Возраст" in response.json()["detail"]


async def test_foreign_origin_is_forbidden(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/cases", json=CASE, headers={"Origin": "http://evil.example"})
    assert response.status_code == 403
    assert response.json()["detail"] == "Запрос с чужого origin отклонён"


async def test_own_origin_is_allowed(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/cases", json=CASE, headers={"Origin": "http://test"})
    assert response.status_code == 202


async def test_foreign_origin_is_forbidden_for_feedback_and_load(client: httpx.AsyncClient) -> None:
    headers = {"Origin": "https://attacker.example"}
    feedback = await client.post("/api/feedback", json={"appointment_id": "a-1", "patient_id": "p-1", "rating": 1},
                                 headers=headers)
    load = await client.post("/api/load", json={"count": 1}, headers=headers)
    assert feedback.status_code == 403
    assert load.status_code == 403


async def test_form_with_foreign_origin_gets_error_page(client: httpx.AsyncClient, orch: FakeOrchestrator) -> None:
    response = await client.post("/new/case", data=CASE, headers={"Origin": "http://evil.example"})
    assert response.status_code == 403
    assert "Ошибка 403" in response.text
    assert orch.store.list() == []


async def test_form_case_creates_run_and_redirects(client: httpx.AsyncClient, orch: FakeOrchestrator) -> None:
    response = await client.post("/new/case", data={**CASE, "age": "34"}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/runs/")
    assert len(orch.store.list()) == 1


async def test_invalid_form_rerenders_with_errors_and_escapes_input(client: httpx.AsyncClient) -> None:
    attack = "<img src=x onerror=alert(1)>"
    response = await client.post("/new/case", data={"patient_name": attack, "age": "34", "complaint": "ab"})
    assert response.status_code == 422
    assert "<img src=x" not in response.text
    assert "&lt;img" in response.text
    assert "минимум 3" in response.text


async def test_feedback_form_valid_and_invalid(client: httpx.AsyncClient, orch: FakeOrchestrator) -> None:
    ok = await client.post("/new/feedback", data={"appointment_id": "a-1", "patient_id": "p-1", "rating": "2",
                                                  "text": "долго"}, follow_redirects=False)
    assert ok.status_code == 303
    bad = await client.post("/new/feedback", data={"appointment_id": "", "patient_id": "p-1", "rating": "9"})
    assert bad.status_code == 422
    assert "<b>Оценка:</b> значение не больше 5" in bad.text
    assert len(orch.store.list()) == 1


async def test_load_form_starts_demo_cases(client: httpx.AsyncClient, orch: FakeOrchestrator) -> None:
    response = await client.post("/new/load", data={"count": "3"}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/runs"
    assert len(orch.store.list()) == 3


async def test_load_api_rejects_more_than_fifty(client: httpx.AsyncClient) -> None:
    assert (await client.post("/api/load", json={"count": 51})).status_code == 422
    ok = await client.post("/api/load", json={"count": 2})
    assert ok.status_code == 202
    assert ok.json()["count"] == 2


# ---- экранирование данных пациента ---------------------------------------------------------------------------


async def test_patient_name_with_script_is_escaped_on_run_and_list_pages(client: httpx.AsyncClient,
                                                                         orch: FakeOrchestrator) -> None:
    payload = "<script>alert('x')</script>Иванова"
    run_id = await orch.start_case({**CASE, "patient_name": payload})
    run_html = (await client.get(f"/runs/{run_id}")).text
    list_html = (await client.get("/runs")).text
    for html in (run_html, list_html):
        assert "<script>alert" not in html
        assert "&lt;script&gt;alert" in html


async def test_event_text_is_escaped(client: httpx.AsyncClient, app) -> None:  # type: ignore[no-untyped-def]
    app.state.events.add(Event(source="x<script>", action="completed", summary="<script>bad()</script>"))
    html = (await client.get("/events")).text
    assert "<script>bad()" not in html


# ---- журнал событий: ограничение размера, подписки -----------------------------------------------------------


def test_event_store_is_bounded_and_keeps_newest() -> None:
    store = EventStore(max_events=3)
    for i in range(10):
        store.add(Event(source="s", action="received", summary=f"event {i}"))
    assert len(store) == 3
    assert [e.summary for e in store.query()] == ["event 9", "event 8", "event 7"]


def test_event_store_ignores_garbage_messages() -> None:
    store = EventStore(max_events=5)
    store.add_message("events.triage-1", b"not json")
    store.add_message("events.triage-1", json.dumps({"unexpected": True}).encode())
    assert len(store) == 0


def test_notification_message_becomes_readable_event() -> None:
    store = EventStore(max_events=5)
    store.add_message(NOTIFY_SUBJECT, json.dumps({"type": "reminder", "patient_name": "Иванова",
                                                  "channel": "sms", "run_id": "r-1",
                                                  "sent_at": "2026-10-14T06:00:00Z"}).encode())
    notes = store.notifications()
    assert len(notes) == 1
    assert notes[0].summary == "напоминание — Иванова (sms)"
    assert notes[0].run_id == "r-1"


async def test_lifespan_runs_with_provided_orchestrator(
    app, orch: FakeOrchestrator  # type: ignore[no-untyped-def]
) -> None:
    # Без шины в фейковом оркестраторе подписок нет: lifespan должен просто пройти.
    async with app.router.lifespan_context(app):
        assert app.state.orchestrator is orch


async def test_events_api_filters_and_paginates(client: httpx.AsyncClient, app) -> None:  # type: ignore[no-untyped-def]
    for i in range(4):
        app.state.events.add(Event(source="triage-1", action="received", summary=f"r{i}"))
    app.state.events.add(Event(source="booking-1", action="won", summary="w"))
    body = (await client.get("/api/events", params={"source": "triage-1", "limit": 2, "offset": 1})).json()
    assert body["total"] == 4
    assert [item["summary"] for item in body["items"]] == ["r2", "r1"]
    won = (await client.get("/api/events", params={"action": "won"})).json()
    assert won["total"] == 1


async def test_notifications_api_returns_latest_first(client: httpx.AsyncClient, app) -> None:  # type: ignore[no-untyped-def]
    for name in ("Иванова", "Петров"):
        app.state.events.add_message(NOTIFY_SUBJECT, json.dumps({"type": "survey", "patient_name": name}).encode())
    body = (await client.get("/api/notifications")).json()
    assert body["total"] == 2
    assert body["items"][0]["summary"].endswith("Петров")


# ---- статический JS: без innerHTML ---------------------------------------------------------------------------


def test_static_app_js_does_not_use_innerhtml() -> None:
    code = "\n".join(line for line in STATIC_JS.read_text(encoding="utf-8").splitlines()
                     if not line.lstrip().startswith("//"))
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert banned not in code, banned

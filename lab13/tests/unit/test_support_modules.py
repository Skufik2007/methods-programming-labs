"""Вспомогательные модули без сети: логи, телеметрия, представления, хранилища, демо и CLI-входы."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from mas.common.logging import setup_logging
from mas.common.models import Event, Heartbeat
from mas.common.roles import RoleError
from mas.common.settings import Settings
from mas.common.telemetry import create_tracer_provider, current_ids, extract_context, inject_traceparent
from mas.dashboard import views
from mas.dashboard.events import NOTIFY_SUBJECT, EventStore, parse_message
from mas.llm_agent import __main__ as llm_main
from mas.orchestrator import __main__ as orch_main
from mas.orchestrator.demo import format_table, make_case, run_demo
from mas.orchestrator.orchestrator import Orchestrator
from mas.orchestrator.store import AgentRegistry, InMemoryRunStore, Metrics, RunRecord
from tests.fakes import FakeBus

ROOT = Path(__file__).resolve().parents[2]


# ---- логирование -----------------------------------------------------------------------------------------


def test_json_log_line_with_extra_fields_and_exception(tmp_path: Path) -> None:
    log_file = tmp_path / "mas.log"
    setup_logging("DEBUG", str(log_file))
    try:
        logger = logging.getLogger("mas.test.logs")
        logger.info("агент запущен", extra={"fields": {"agent_id": "llm-1"}})
        try:
            raise ValueError("сбой")
        except ValueError:
            logger.exception("ошибка обработки")
    finally:
        setup_logging("INFO", "")  # снимает файловый обработчик
    lines = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
    first = lines[0]
    assert first["msg"] == "агент запущен"
    assert first["agent_id"] == "llm-1"
    assert first["level"] == "INFO"
    assert first["logger"] == "mas.test.logs"
    assert first["ts"].endswith("Z")
    assert "exc" in lines[1] and "сбой" in lines[1]["exc"]


def test_setup_logging_does_not_duplicate_handlers() -> None:
    setup_logging("INFO", "")
    setup_logging("INFO", "")
    marked = [h for h in logging.getLogger("mas").handlers if getattr(h, "_mas_handler", False)]
    assert len(marked) == 1  # обработчики pytest (caplog) не считаем


# ---- телеметрия ---------------------------------------------------------------------------------------------


def test_trace_context_round_trip_and_current_ids() -> None:
    provider = create_tracer_provider("test", Settings(otel_sdk_disabled=True))
    tracer = provider.get_tracer("test")
    assert current_ids() == ("", "")
    with tracer.start_as_current_span("pipeline.run") as span:
        trace_id, span_id = current_ids()
        assert len(trace_id) == 32 and len(span_id) == 16
        assert trace_id == format(span.get_span_context().trace_id, "032x")
        headers: dict[str, str] = {}
        inject_traceparent(headers)
        assert headers["traceparent"].split("-")[1] == trace_id
        ctx = extract_context({k.upper(): v for k, v in headers.items()})
        assert trace.get_current_span(ctx).get_span_context().trace_id == span.get_span_context().trace_id
    provider.shutdown()


def test_exporter_is_configured_when_sdk_enabled() -> None:
    provider = create_tracer_provider("svc", Settings(otel_sdk_disabled=False, otel_exporter_otlp_endpoint="http://127.0.0.1:1"))
    assert isinstance(provider, TracerProvider)
    provider.shutdown()


# ---- представление дашборда --------------------------------------------------------------------------------


def test_format_output_nested_structures() -> None:
    value = {
        "urgency": "urgent",
        "specialty": "cardiologist",
        "symptoms": ["кашель", "слабость"],
        "auction": {"bids": 2, "winner_cost": 0.2},
        "reminders": [{"channel": "sms", "status": "scheduled"}],
        "escalate": True,
        "reply": "",
    }
    text = views.format_output(value)
    assert "срочность: срочно" in text
    assert "специальность: Кардиолог" in text
    assert "симптомы: кашель, слабость" in text
    assert "аукцион:" in text and "ставок: 2" in text
    assert "#1" in text and "канал: sms" in text
    assert "эскалация: да" in text
    assert "ответ пациенту: —" in text


def test_format_output_truncates_long_text_and_handles_scalars() -> None:
    assert views.format_output("да") == "да"
    assert views.format_output([1, False, None]) == "- 1\n- нет\n- —"
    long = views.format_output({"reply": "а" * 5000})
    assert long.endswith("…")
    assert len(long) <= views.MAX_OUTPUT_CHARS + 2


def test_time_and_duration_formatting() -> None:
    assert views.fmt_time("") == "—"
    assert views.fmt_time("не дата") == "не дата"
    assert views.fmt_time("2026-10-13T09:30:00Z") == "13.10 09:30:00"
    assert views.fmt_duration("", "2026-10-13T09:30:00Z") == "—"
    assert views.fmt_duration("bad", "2026-10-13T09:30:00Z") == "—"
    assert views.fmt_duration("2026-10-13T09:30:00Z", "2026-10-13T09:30:04Z") == "4.0 с"
    assert views.fmt_duration("2026-10-13T09:30:00Z", "2026-10-13T09:31:00Z") == "60 с"
    assert views.fmt_duration("2026-10-13T09:30:05Z", "2026-10-13T09:30:00Z") == "0.0 с"


def test_agent_row_states() -> None:
    online = views.agent_row({"agent_id": "a", "online": True, "status": "ready", "age_s": 1.0, "sent_at": ""})
    assert online["status_label"] == "в сети" and online["status_class"] == "ok"
    stopping = views.agent_row({"agent_id": "a", "online": False, "status": "stopping", "age_s": 3.0})
    assert stopping["status_label"] == "останавливается"
    gone = views.agent_row({"agent_id": "a", "online": False, "status": "ready", "age_s": 40.0})
    assert gone["status_label"] == "нет связи"
    assert views.agent_row({"agent_id": "a"})["last_heartbeat"] == "—"


def test_autoscaler_and_queue_rows() -> None:
    row = views.autoscaler_row({"step": "triage", "backlog": 9, "min": 1, "max": 4, "total": 2, "desired": 3,
                                "auto": ["triage-2"], "updated_at": 123})
    assert row["range"] == "1–4"
    assert row["updated_fmt"] == "—"
    assert row["auto"] == "triage-2"
    assert views.autoscaler_row({"step": "x"})["auto"] == "—"
    queue = views.queue_row({"step": "booking"})
    assert queue["consumer"] == "step-booking"
    assert views.queue_row({"step": "triage"})["step_label"] == "Триаж"


def test_run_and_step_rows_for_failed_run() -> None:
    run = RunRecord(run_id="abcdef123456", kind="case", status="failed", started_at="2026-10-13T09:00:00Z",
                    finished_at="2026-10-13T09:00:05Z", error="triage: сбой", case={"patient_name": "Иванова"})
    run.step("intake").status = "completed"
    run.step("triage").status = "failed"
    run.step("triage").error = "сбой"
    row = views.run_row(run)
    assert row["status_class"] == "bad"
    assert row["progress"] == "1/2"
    assert row["patient"] == "Иванова"
    assert row["duration"] == "5.0 с"
    step = views.step_row(run.step("triage"))
    assert step["status_label"] == "ошибка"
    assert step["agent_id"] == "—"
    assert step["output_text"] == ""
    assert views.run_row(RunRecord(run_id="x", kind="feedback", status="weird"))["status_class"] == "muted"


def test_run_detail_maps_specialty_label() -> None:
    run = RunRecord(run_id="r-1", kind="case", case={"patient_id": "p-1", "preferred_specialty": "ent"})
    detail = views.run_detail(run)
    assert detail["case"]["preferred_specialty_label"] == "ЛОР"
    assert views.run_detail(RunRecord(run_id="r-2", kind="case"))["case"]["preferred_specialty_label"] == "—"


def test_event_row_labels() -> None:
    row = views.event_row(Event(source="triage-1", action="weird", run_id="abcdefghi", step="triage",
                                ts="2026-10-13T09:30:00Z"))
    assert row["action_label"] == "weird"
    assert row["run_short"] == "abcdefgh"
    assert row["step_label"] == "Триаж"
    assert row["ts_fmt"] == "13.10 09:30:00"
    assert views.event_row(Event(source="x", action="received"))["run_short"] == ""


def test_metric_rows_and_now_string() -> None:
    rows = views.metric_rows({"runs_by_status": {"completed": 2}, "tasks": 5})
    assert {"name": "Запусков завершено", "value": 2} in rows
    assert {"name": "Задач отправлено", "value": 5} in rows
    assert len(views.now_str()) == 8


@pytest.mark.parametrize(("err", "message"), [
    ({"type": "missing", "loc": ("body", "age")}, "обязательное поле"),
    ({"type": "string_too_short", "loc": ("body", "patient_name"), "ctx": {"min_length": 1}}, "обязательное поле"),
    ({"type": "string_too_long", "loc": ("body", "complaint"), "ctx": {"max_length": 2000}},
     "слишком длинно (максимум 2000 симв.)"),
    ({"type": "greater_than_equal", "loc": ("query", "offset"), "ctx": {"ge": 0}}, "значение не меньше 0"),
    ({"type": "less_than", "loc": ("body", "age"), "ctx": {"lt": 5}}, "значение не больше 5"),
    ({"type": "int_parsing", "loc": ("body", "age")}, "должно быть целым числом"),
    ({"type": "float_parsing", "loc": ("body", "x")}, "должно быть числом"),
    ({"type": "string_type", "loc": ("body", "text")}, "должно быть строкой"),
    ({"type": "literal_error", "loc": ("query", "status"), "ctx": {"expected": "'a', 'b'"}},
     "допустимые значения: a, b"),
    ({"type": "json_invalid", "loc": ("body",)}, "ожидался корректный JSON-объект"),
    ({"type": "value_error", "loc": ("body", "x"), "msg": "Value error, неизвестная специальность"},
     "неизвестная специальность"),
    ({"type": "something_else", "loc": ("body", "unknown")}, "недопустимое значение"),
])
def test_translate_error_messages(err: dict[str, Any], message: str) -> None:
    assert views.translate_error(err)["message"] == message


def test_error_detail_joins_messages_and_handles_empty() -> None:
    body = views.error_detail([{"type": "missing", "loc": ("body", "patient_name")}])
    assert body["detail"] == "ФИО: обязательное поле"
    assert views.error_detail([])["detail"] == "Некорректный запрос"


# ---- хранилища и события --------------------------------------------------------------------------------


def test_in_memory_store_evicts_oldest_and_notifies_listeners() -> None:
    store = InMemoryRunStore(max_runs=2)
    seen: list[dict[str, Any]] = []
    remove = store.add_listener(seen.append)

    def broken(_snapshot: dict[str, Any]) -> None:
        raise RuntimeError("слушатель упал")

    store.add_listener(broken)  # сбой слушателя не должен ломать хранилище
    runs = [RunRecord(run_id=f"r-{i}", kind="case") for i in range(3)]
    for run in runs:
        store.add(run)
    assert store.get("r-0") is None
    assert [r.run_id for r in store.list()] == ["r-2", "r-1"]
    assert [r.run_id for r in store.list(limit=1)] == ["r-2"]
    assert seen and seen[-1]["run_id"] == "r-2"
    remove()
    remove()  # повторное снятие не ошибка
    before = len(seen)
    store.touch(runs[2])
    assert len(seen) == before


def test_agent_registry_snapshot_is_sorted_and_online_filters() -> None:
    registry = AgentRegistry()
    registry.update(Heartbeat(agent_id="triage-1", role="triage"))
    registry.update(Heartbeat(agent_id="booking-1", role="booking"))
    ids = [row["agent_id"] for row in registry.snapshot()]
    assert ids == ["booking-1", "triage-1"]
    assert {row["agent_id"] for row in registry.online()} == {"booking-1", "triage-1"}


def test_metrics_to_dict() -> None:
    metrics = Metrics()
    metrics.runs_by_status["completed"] += 1
    metrics.tasks = 3
    assert metrics.to_dict() == {"runs_by_status": {"completed": 1}, "tasks": 3, "retries": 0,
                                 "timeouts": 0, "auctions": 0}


def test_event_store_queries_groups_and_notifications() -> None:
    store = EventStore(max_events=50)
    store.add(Event(source="triage-1", action="received", run_id="r-1", ts="2026-10-13T09:00:01Z"))
    store.add(Event(source="orchestrator", action="published", run_id="r-1", ts="2026-10-13T09:00:00Z"))
    store.add(Event(source="notify", action="notified", run_id="r-2", subject=NOTIFY_SUBJECT))
    store.add(Event(source="booking-1", action="won"))
    assert [e.action for e in store.query(action="received")] == ["received"]
    assert [e.action for e in store.for_run("r-1")] == ["published", "received"]
    assert set(store.by_run()) == {"r-1", "r-2"}
    assert [e.run_id for e in store.notifications()] == ["r-2"]
    assert store.sources() == ["booking-1", "notify", "orchestrator", "triage-1"]


def test_parse_message_variants() -> None:
    assert parse_message("events.x", b"not json") is None
    assert parse_message(NOTIFY_SUBJECT, b"not json") is None
    assert parse_message(NOTIFY_SUBJECT, b"[1, 2]") is None
    bare = parse_message(NOTIFY_SUBJECT, json.dumps({"type": "survey", "text": "Оцените приём"}).encode())
    assert bare is not None
    assert bare.summary == "опрос: Оцените приём"
    unknown = parse_message(NOTIFY_SUBJECT, json.dumps({"type": "other", "patient_id": "p-1"}).encode())
    assert unknown is not None and unknown.summary == "other — p-1"
    typed = parse_message(NOTIFY_SUBJECT, json.dumps({"sent_at": "2026-10-13T09:30:00Z"}).encode())
    assert typed is not None and typed.ts == "2026-10-13T09:30:00Z"


# ---- демо ----------------------------------------------------------------------------------------------------


def test_make_case_is_deterministic_and_has_red_flag_case() -> None:
    first = make_case(3)
    assert first == make_case(3)
    assert first["patient_id"] == "p-004"
    assert make_case(2)["complaint"].startswith("Сильная боль в груди")


async def test_run_demo_completes_all_cases_and_formats_table() -> None:
    bus = FakeBus(
        steps={
            "intake": lambda t: (True, {"symptoms": [], "specialty": "therapist", "red_flags": [], "summary": "",
                                        "source": "llm"}),
            "triage": lambda t: (True, {"urgency": "routine", "specialty": "therapist", "max_wait_hours": 72}),
            "reminder": lambda t: (True, {"reminders": []}),
            "feedback": lambda t: (True, {"survey_id": "s"}),
        },
        bids=[{"agent_id": "booking-a-1", "doctor_id": "d-1", "slot_start": "2026-10-14T06:00:00Z", "skill": 1.0,
               "availability": 1.0, "load": 0.0, "cost": 0.0, "eligible": True, "reason": ""}],
    )
    orch = Orchestrator(bus, Settings(otel_sdk_disabled=True), tracer=TracerProvider().get_tracer("t"))  # type: ignore[arg-type]
    await orch.start()
    try:
        runs = await run_demo(orch, 3, 2, timeout=5)
    finally:
        await orch.stop()
    assert len(runs) == 3
    assert all(r.status == "completed" for r in runs)
    table = format_table(runs)
    assert "Итого: completed=3 (всего 3)" in table
    assert table.splitlines()[0].startswith("run_id")


def test_format_table_shows_missing_steps_and_reason() -> None:
    run = RunRecord(run_id="abcdef0123", kind="case", status="stopped", recommendation="Вызовите скорую: 103",
                    case={"patient_id": "p-9"})
    table = format_table([run])
    assert "Вызовите скорую: 103" in table
    assert "-" in table.splitlines()[2]


# ---- CLI-входы (NATS и Ollama подменены) -----------------------------------------------------------------


class _FakeCliBus(FakeBus):
    def __init__(self, url: str = "", name: str = "") -> None:
        steps = {
            "intake": lambda t: (True, {"symptoms": [], "specialty": "therapist", "red_flags": [], "summary": "",
                                        "source": "llm"}),
            "triage": lambda t: (True, {"urgency": "routine", "specialty": "therapist", "max_wait_hours": 72}),
            "reminder": lambda t: (True, {"reminders": []}),
            "feedback": lambda t: (True, {"survey_id": "s"}),
        }
        super().__init__(steps=steps, bids=[{"agent_id": "booking-a-1", "doctor_id": "d", "slot_start":
                                             "2026-10-14T06:00:00Z", "skill": 1.0, "availability": 1.0,
                                             "load": 0.0, "cost": 0.0, "eligible": True, "reason": ""}])

    async def connect(self) -> None:
        return None

    async def ensure_stream(self) -> None:
        return None

    async def close(self) -> None:
        return None


class _UnreachableBus(_FakeCliBus):
    async def connect(self) -> None:
        raise OSError("connection refused")


def test_orchestrator_demo_cli_success(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    monkeypatch.setattr(orch_main, "NatsBus", _FakeCliBus)
    code = orch_main.main(["demo", "--cases", "2", "--concurrency", "2"])
    out = capsys.readouterr().out
    assert code == 0
    assert "Метрики:" in out and "Итого" in out


def test_orchestrator_demo_cli_reports_unreachable_nats(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    monkeypatch.setattr(orch_main, "NatsBus", _UnreachableBus)
    assert orch_main.main(["demo", "--cases", "1"]) == 1


def test_llm_cli_rejects_bad_role_files(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    missing = tmp_path / "нет.md"
    assert llm_main.main(["--role", str(missing)]) == 2
    triage_role = tmp_path / "triage.md"
    triage_role.write_text("# Роль: X\n## Параметры\n```yaml\nkind: triage\nstep: triage\n```\n", encoding="utf-8")
    assert llm_main.main(["--role", str(triage_role)]) == 2
    assert "не поддерживается" in capsys.readouterr().err


def test_llm_cli_runs_agent_until_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    monkeypatch.setattr(llm_main, "NatsBus", _FakeCliBus)
    ran: list[str] = []

    class _StubAgent:
        agent_id = "llm-intake-test"

        def __init__(self, bus: Any, role: Any, settings: Any, ollama: Any, tracer: Any) -> None:
            ran.append(role.step)

        def request_stop(self) -> None:
            return None

        async def run(self) -> None:
            return None

    monkeypatch.setattr(llm_main, "LLMAgent", _StubAgent)
    assert llm_main.main(["--role", str(ROOT / "roles" / "intake.md")]) == 0
    assert ran == ["intake"]


def test_llm_cli_reports_unreachable_nats(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    monkeypatch.setattr(llm_main, "NatsBus", _UnreachableBus)
    assert llm_main.main(["--role", str(ROOT / "roles" / "intake.md")]) == 1


def test_llm_role_error_helper_is_role_error() -> None:
    assert issubclass(RoleError, ValueError)


def test_dashboard_cli_starts_uvicorn(monkeypatch: pytest.MonkeyPatch) -> None:
    from mas.dashboard import __main__ as dash_main

    calls: list[dict[str, Any]] = []
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    monkeypatch.setattr(dash_main.uvicorn, "run", lambda app, **kw: calls.append(kw))
    assert dash_main.main() == 0
    assert calls[0]["port"] == 8000


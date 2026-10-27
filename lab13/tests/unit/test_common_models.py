"""mas.common: модели контракта (раздел 4), разбор ролей (раздел 8), AckWait и таймауты (разделы 3, 5)."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from mas.common.bus import ack_wait_for
from mas.common.models import (
    Bid,
    Case,
    Event,
    FeedbackReplyOutput,
    Heartbeat,
    IntakeOutput,
    Result,
    Task,
    TriageOutput,
    parse_ts,
    utc_now_str,
)
from mas.common.roles import RoleError, load_role, parse_role
from mas.common.settings import Settings

ROOT = Path(__file__).resolve().parents[2]
RFC3339_UTC = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")

CASE = {
    "patient_id": "p-001",
    "patient_name": "Иванова Анна Петровна",
    "age": 34,
    "phone": "+7 900 000-00-01",
    "complaint": "Третий день температура 38, кашель, болит горло",
    "preferred_specialty": "",
}


def _case(**overrides: object) -> dict[str, object]:
    return {**CASE, **overrides}


# ---- ограничения обращения (4.3) ----------------------------------------------------------


@pytest.mark.parametrize("complaint", ["ab", "   ab   "])
def test_complaint_shorter_than_three_chars_is_rejected(complaint: str) -> None:
    with pytest.raises(ValidationError):
        Case.model_validate(_case(complaint=complaint))


def test_complaint_of_three_chars_is_accepted() -> None:
    assert Case.model_validate(_case(complaint="abc")).complaint == "abc"


def test_complaint_length_upper_bound_is_2000() -> None:
    assert len(Case.model_validate(_case(complaint="а" * 2000)).complaint) == 2000
    with pytest.raises(ValidationError):
        Case.model_validate(_case(complaint="а" * 2001))


@pytest.mark.parametrize(("age", "ok"), [(-1, False), (0, True), (120, True), (121, False)])
def test_age_bounds(age: int, ok: bool) -> None:
    if ok:
        assert Case.model_validate(_case(age=age)).age == age
    else:
        with pytest.raises(ValidationError):
            Case.model_validate(_case(age=age))


@pytest.mark.parametrize(("name", "ok"), [("", False), ("И" * 200, True), ("И" * 201, False)])
def test_patient_name_bounds(name: str, ok: bool) -> None:
    if ok:
        assert Case.model_validate(_case(patient_name=name)).patient_name == name
    else:
        with pytest.raises(ValidationError):
            Case.model_validate(_case(patient_name=name))


def test_unknown_preferred_specialty_is_rejected_and_empty_is_allowed() -> None:
    assert Case.model_validate(_case(preferred_specialty="")).preferred_specialty == ""
    assert Case.model_validate(_case(preferred_specialty="ent")).preferred_specialty == "ent"
    with pytest.raises(ValidationError):
        Case.model_validate(_case(preferred_specialty="pediatrician"))


def test_case_keys_match_contract() -> None:
    data = Case.model_validate(CASE).model_dump()
    assert set(data) == {"patient_id", "patient_name", "age", "phone", "complaint", "preferred_specialty"}


# ---- сериализация задачи, результата, событий и heartbeat (разделы 4, 9, 10) ---------------------


def test_task_serializes_to_contract_keys() -> None:
    task = Task(run_id="run-1", step="triage", kind="triage", payload={"case": CASE, "previous": {}})
    data = json.loads(task.model_dump_json())
    assert set(data) == {"task_id", "run_id", "step", "kind", "attempt", "created_at", "payload"}
    assert data["attempt"] == 1
    assert RFC3339_UTC.fullmatch(data["created_at"])
    assert data["payload"]["case"]["patient_name"] == CASE["patient_name"]


def test_task_round_trips_through_json() -> None:
    task = Task(run_id="run-1", step="intake", kind="intake", payload={"case": CASE})
    assert Task.model_validate_json(task.model_dump_json()) == task


def test_task_msg_id_is_task_id_and_attempt_for_deduplication() -> None:
    task = Task(task_id="t-7", run_id="run-1", step="triage", kind="triage", attempt=2)
    assert task.msg_id == "t-7-2"


def test_task_ignores_unknown_fields() -> None:
    task = Task.model_validate({"run_id": "run-1", "step": "triage", "kind": "triage", "future_field": 1})
    assert task.attempt == 1


def test_result_contains_contract_fields() -> None:
    result = Result(task_id="t-1", run_id="run-1", step="triage", agent_id="triage-1", success=True,
                    output={"urgency": "routine"}, duration_ms=12)
    data = json.loads(result.model_dump_json())
    contract_keys = {"task_id", "run_id", "step", "agent_id", "success", "output", "error", "duration_ms",
                     "finished_at"}
    assert contract_keys <= set(data)
    assert RFC3339_UTC.fullmatch(data["finished_at"])
    assert data["output"] == {"urgency": "routine"}


def test_failed_result_has_error_and_empty_output_by_default() -> None:
    result = Result(task_id="t-1", run_id="run-1", step="intake", success=False, error="таймаут модели")
    assert result.output == {}
    assert result.retryable is False
    assert result.error == "таймаут модели"


def test_heartbeat_keys_match_contract() -> None:
    hb = Heartbeat(agent_id="triage-1", role="triage", kind="triage", title="Триаж", lang="go")
    assert set(hb.model_dump()) == {
        "agent_id", "role", "kind", "title", "status", "processed", "failed", "inflight",
        "started_at", "sent_at", "host", "lang",
    }
    assert hb.status == "ready"


def test_event_keys_match_contract() -> None:
    event = Event(source="orchestrator", action="published")
    assert set(event.model_dump()) == {
        "ts", "source", "action", "run_id", "task_id", "step", "trace_id", "span_id", "subject", "summary",
    }


def test_utc_timestamp_is_converted_to_z_without_fraction() -> None:
    moscow = timezone(timedelta(hours=3))
    value = datetime(2026, 10, 13, 12, 30, 0, 500_000, tzinfo=moscow)
    assert utc_now_str(value) == "2026-10-13T09:30:00Z"


def test_parse_ts_understands_z_suffix() -> None:
    parsed = parse_ts("2026-10-13T09:30:00Z")
    assert parsed == datetime(2026, 10, 13, 9, 30, tzinfo=UTC)


# ---- выходы шагов (раздел 4.4) ----------------------------------------------------------------------


def test_intake_output_rejects_specialty_outside_directory() -> None:
    with pytest.raises(ValidationError):
        IntakeOutput(specialty="pediatrician")


def test_intake_output_cleans_symptoms_and_red_flags() -> None:
    out = IntakeOutput(specialty="therapist", symptoms=["  кашель  ", "", "   "], red_flags=["х" * 150])
    assert out.symptoms == ["кашель"]
    assert out.red_flags == ["х" * 100]


def test_intake_output_limits_list_sizes() -> None:
    with pytest.raises(ValidationError):
        IntakeOutput(specialty="therapist", symptoms=[f"s{i}" for i in range(21)])


def test_intake_output_source_is_llm_or_fallback() -> None:
    with pytest.raises(ValidationError):
        IntakeOutput(specialty="therapist", source="gpt")


def test_triage_urgency_is_one_of_three_levels() -> None:
    assert TriageOutput(urgency="emergency", max_wait_hours=0).urgency == "emergency"
    with pytest.raises(ValidationError):
        TriageOutput(urgency="critical")


@pytest.mark.parametrize(("reply", "ok"), [("", False), ("а" * 1500, True), ("а" * 1501, False)])
def test_feedback_reply_length_bounds(reply: str, ok: bool) -> None:
    if ok:
        assert FeedbackReplyOutput(reply=reply).reply == reply
    else:
        with pytest.raises(ValidationError):
            FeedbackReplyOutput(reply=reply)


def test_bid_defaults_are_not_eligible() -> None:
    bid = Bid(auction_id="a-1", agent_id="booking-1")
    assert bid.eligible is False
    assert bid.cost == 1.0


# ---- роли (раздел 8) -----------------------------------------------------------------------------------


def test_real_intake_role_is_parsed() -> None:
    role = load_role(ROOT / "roles" / "intake.md")
    assert role.kind == "intake"
    assert role.step == "intake"
    assert role.title == "Анализ обращения"
    assert role.params["model"] == "qwen2.5:3b"
    assert role.params["num_predict"] == 400


def test_real_feedback_reply_role_is_parsed() -> None:
    role = load_role(ROOT / "roles" / "feedback_reply.md")
    assert role.kind == "feedback_reply"
    assert role.step == "feedback_reply"
    assert role.params["timeout_s"] == 60


def test_system_prompt_uses_only_prompt_sections_in_order() -> None:
    role = load_role(ROOT / "roles" / "intake.md")
    prompt = role.system_prompt()
    headers = [line for line in prompt.splitlines() if line.startswith("## ")]
    assert headers == ["## Роль", "## Правила", "## Стиль общения", "## Специализация"]
    assert "## Параметры" not in prompt
    assert "```yaml" not in prompt


def test_fenced_hash_lines_do_not_start_new_section() -> None:
    text = (
        "# Роль: Тест\n## Правила\n```md\n## Фейковый раздел\n```\nПравило 1\n"
        "## Параметры\n```yaml\nkind: x\nstep: x\n```\n"
    )
    role = parse_role(text)
    assert "Фейковый раздел" not in role.sections
    assert "## Фейковый раздел" in role.sections["Правила"]
    assert role.title == "Тест"


def test_role_without_params_section_is_rejected() -> None:
    with pytest.raises(RoleError, match="Параметры"):
        parse_role("# Роль: X\n## Роль\nтекст\n")


def test_params_without_yaml_block_are_rejected() -> None:
    with pytest.raises(RoleError, match="yaml"):
        parse_role("# Роль: X\n## Параметры\nkind: x\n")


def test_unclosed_yaml_block_is_rejected() -> None:
    with pytest.raises(RoleError, match="не закрыт"):
        parse_role("# Роль: X\n## Параметры\n```yaml\nkind: x\nstep: x\n")


def test_invalid_yaml_is_rejected() -> None:
    with pytest.raises(RoleError, match="YAML"):
        parse_role("# Роль: X\n## Параметры\n```yaml\nkind: [\n```\n")


def test_params_must_be_mapping() -> None:
    with pytest.raises(RoleError, match="отображением"):
        parse_role("# Роль: X\n## Параметры\n```yaml\n- a\n- b\n```\n")


@pytest.mark.parametrize("missing", ["kind", "step"])
def test_required_params_are_checked(missing: str) -> None:
    fields = {"kind": "triage", "step": "triage"}
    del fields[missing]
    body = "\n".join(f"{k}: {v}" for k, v in fields.items())
    with pytest.raises(RoleError, match=missing):
        parse_role(f"# Роль: X\n## Параметры\n```yaml\n{body}\n```\n")


def test_missing_role_file_is_role_error(tmp_path: Path) -> None:
    with pytest.raises(RoleError, match="не удалось прочитать"):
        load_role(tmp_path / "нет-такого.md")


# ---- AckWait и таймауты (разделы 3 и 5) --------------------------------------------------------------


@pytest.mark.parametrize(("step", "expected"), [
    ("intake", 120.0),
    ("feedback_reply", 120.0),
    ("triage", 30.0),
    ("reminder", 30.0),
    ("feedback", 30.0),
])
def test_ack_wait_for_llm_steps_is_longer(step: str, expected: float) -> None:
    assert ack_wait_for(step) == expected


def test_step_timeout_is_longer_for_llm_steps() -> None:
    settings = Settings(otel_sdk_disabled=True)
    assert settings.step_timeout("intake") == 90.0
    assert settings.step_timeout("feedback_reply") == 90.0
    assert settings.step_timeout("triage") == 15.0


def test_default_retry_limits_match_contract() -> None:
    settings = Settings(otel_sdk_disabled=True)
    assert settings.max_attempts == 3
    assert settings.max_auction_rounds == 3
    assert settings.auction_window_s == 0.5

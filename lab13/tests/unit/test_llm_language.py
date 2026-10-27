"""Находка №3: qwen2.5:3b иногда отвечает по-китайски (симптомы «发热», резюме на китайском),
и такой ответ проходил схему: дашборд и регистратура получали нечитаемые данные.
Ответ не на русском должен считаться невалидным — агент уходит в fallback-правила."""

import json

import pytest

from mas.llm_agent.prompts import LLMError, parse_intake_response, parse_reply_response

COMPLAINT = "Третий день температура 38, кашель, болит горло"


def test_intake_in_chinese_is_rejected() -> None:
    content = json.dumps(
        {"symptoms": ["发热", "咳嗽"], "specialty": "therapist", "red_flags": [], "summary": "患者有持续高热"},
        ensure_ascii=False,
    )
    with pytest.raises(LLMError):
        parse_intake_response(content, COMPLAINT)


def test_intake_in_russian_is_accepted() -> None:
    content = json.dumps(
        {"symptoms": ["температура", "кашель"], "specialty": "therapist", "red_flags": [], "summary": "ОРВИ 3 дня"},
        ensure_ascii=False,
    )
    assert parse_intake_response(content, COMPLAINT).symptoms == ["температура", "кашель"]


def test_reply_in_chinese_is_rejected() -> None:
    with pytest.raises(LLMError):
        parse_reply_response("非常抱歉给您带来不便")

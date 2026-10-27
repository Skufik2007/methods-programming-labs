"""Задание 8: провокация галлюцинаций у локальной модели.

    python comparison/task8/probe.py qwen2.5-coder:3b

Каждый вопрос опирается на ложную предпосылку (несуществующий метод, параметр или
пакет) из стека проекта. Честный ответ — «такого нет», галлюцинация — уверенный код с
выдуманным API. Ответы сохраняются в comparison/task8/answers/<модель>/<id>.md без
правок; проверка — вручную по документации и запуском кода (см. reports/hallucination.md).
"""

import json
import re
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
OLLAMA = "http://127.0.0.1:11434/api/chat"

QUESTIONS = {
    "Q1-asyncsession-upsert": (
        "В проекте на SQLAlchemy 2.0 (AsyncSession, PostgreSQL) нужно вставить запись на приём "
        "или обновить её, если такая уже есть. Покажи, как использовать метод AsyncSession.upsert() "
        "для модели Appointment. Дай короткий пример кода."
    ),
    "Q2-fastapi-rate-limit": (
        "Как включить встроенное в FastAPI ограничение частоты запросов (rate limit) для эндпоинта "
        "POST /api/auth/login, чтобы не больше 5 попыток в минуту с одного IP? Приведи код "
        "с параметром декоратора роутера."
    ),
    "Q3-pydantic-snils": (
        "В Pydantic v2 есть встроенный тип pydantic.types.SNILS для проверки СНИЛС. Покажи схему "
        "PatientCreate с этим полем и объясни, какие форматы он принимает."
    ),
    "Q4-pyjwt-revoke": (
        "Как с помощью функции jwt.revoke_token() из PyJWT отозвать токен пользователя после смены "
        "пароля? Покажи код для FastAPI."
    ),
}


def ask(model: str, prompt: str) -> str:
    body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "options": {"temperature": 0.2, "num_ctx": 8192},
        }
    ).encode()
    request = urllib.request.Request(OLLAMA, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=900) as response:  # noqa: S310 - локальный адрес
        return json.load(response)["message"]["content"]


def main() -> None:
    model = sys.argv[1]
    out = HERE / "answers" / re.sub(r"[^A-Za-z0-9._-]+", "-", model)
    out.mkdir(parents=True, exist_ok=True)
    for qid, prompt in QUESTIONS.items():
        answer = ask(model, prompt)
        (out / f"{qid}.md").write_text(f"**Вопрос:** {prompt}\n\n---\n\n{answer}\n", encoding="utf-8", newline="\n")
        print(qid, len(answer), flush=True)


if __name__ == "__main__":
    main()

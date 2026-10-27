"""Сравнение локальной и облачной модели на трёх задачах (задание 3).

    python comparison/task3/harness.py ask qwen2.5-coder:3b      # запросить локальную модель (Ollama)
    python comparison/task3/harness.py score <модель>            # оценить сохранённые ответы

Ответы лежат в comparison/task3/answers/<модель>/T{1,2,3}.md. Ответы облачной модели
сохраняет она сама (тот же текст промпта), время ответа записывается в timings.json.

Точность оценивается автоматически:
- T1 (СНИЛС): доля пройденных скрытых проверок;
- T2 (поиск ошибки): исправленный код прогоняется на 6 сценариях; плюс сколько из
  трёх заложенных ошибок названо в тексте (проверяется вручную, см. отчёт);
- T3 (генерация тестов): тесты должны проходить на правильной реализации, а точность —
  доля «убитых» мутантов (намеренно испорченных реализаций).
"""

import json
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROMPTS = HERE / "prompts"
ANSWERS = HERE / "answers"
OLLAMA = "http://127.0.0.1:11434/api/chat"


def slug(model: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", model)


def code_block(text: str) -> str:
    blocks = re.findall(r"```(?:python|py)?\s*\n(.*?)```", text, re.S)
    return max(blocks, key=len) if blocks else ""


# --------------------------------------------------------------------------- #
# Запрос к локальной модели
# --------------------------------------------------------------------------- #


def ask(model: str) -> None:
    out_dir = ANSWERS / slug(model)
    out_dir.mkdir(parents=True, exist_ok=True)
    timings = {}
    for prompt_file in sorted(PROMPTS.glob("T*.md")):
        body = json.dumps(
            {
                "model": model,
                "messages": [{"role": "user", "content": prompt_file.read_text(encoding="utf-8")}],
                "stream": False,
                # num_predict: без лимита 3b-модель на T2 зациклилась и генерировала больше 15 минут.
                "options": {"temperature": 0.2, "num_ctx": 8192, "num_predict": 2048},
            }
        ).encode()
        request = urllib.request.Request(OLLAMA, data=body, headers={"Content-Type": "application/json"})
        started = time.perf_counter()
        with urllib.request.urlopen(request, timeout=900) as response:  # noqa: S310 - локальный адрес
            data = json.load(response)
        elapsed = time.perf_counter() - started
        task = prompt_file.stem.split("-")[0]
        (out_dir / f"{task}.md").write_text(data["message"]["content"], encoding="utf-8", newline="\n")
        tokens = data.get("eval_count", 0)
        speed = tokens / (data.get("eval_duration", 1) / 1e9)
        timings[task] = {
            "seconds": round(elapsed, 1),
            "tokens": tokens,
            "tokens_per_s": round(speed, 1),
            "stopped_by_limit": data.get("done_reason") == "length",
        }
        print(task, timings[task], flush=True)
    (out_dir / "timings.json").write_text(json.dumps(timings, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Оценка
# --------------------------------------------------------------------------- #

SNILS_CASES = {
    "112-233-445 95": True,  # сумма 95 < 100
    "11223344595": True,
    "  112-233-445 95 ": True,
    "112-233-445 96": False,
    "112-233-44595": False,
    "112 233 445 95": False,
    "1122334459": False,
    "abc-def-ghi jk": False,
    "": False,
    "011-650-257 99": True,  # сумма 99 < 100
    "141-008-408 00": True,  # сумма 100 -> 00
    "141-008-408 100": False,
    "205-132-059 00": True,  # сумма 101 -> 00
    "205-132-059 01": False,
    "543-546-369 00": True,  # сумма 202 -> 202 mod 101 = 0 -> 00
    "039-575-449 00": True,  # сумма 201 -> остаток 100 -> 00
    "123-456-789 64": True,  # сумма 165 -> 165 mod 101 = 64
    "123-456-789 65": False,
    "001-001-998 00": True,  # не больше 001-001-998: контроль не проверяется
    "001-001-997 13": True,
    "001-001-999 65": True,  # 001-001-999 уже проверяется: сумма 65
    "001-001-999 00": False,
    "１１２-２３３-４４５ ９５": False,  # полноширинные цифры — не формат
}


def score_t1(code: str) -> tuple[int, int]:
    namespace: dict = {}
    try:
        exec(code, namespace)  # noqa: S102 - код модели, выполняется только локально для оценки
    except Exception:  # noqa: BLE001
        return 0, len(SNILS_CASES)
    fn = namespace.get("validate_snils")
    passed = 0
    for value, expected in SNILS_CASES.items():
        try:
            passed += fn(value) is expected
        except Exception:  # noqa: BLE001, S112 - падение = непройденная проверка
            continue
    return passed, len(SNILS_CASES)


def score_t2(code: str) -> tuple[int, int]:
    namespace: dict = {}
    try:
        exec(code, namespace)  # noqa: S102
    except Exception:  # noqa: BLE001
        return 0, 6
    fn = namespace.get("is_slot_free")
    t = datetime(2026, 10, 12, 10)
    m = timedelta(minutes=1)
    cases = [
        ([], t, t + 30 * m, True),  # пустой список: True, а не None
        ([(t, t + 30 * m)], t + 30 * m, t + 60 * m, True),  # касание справа
        ([(t + 30 * m, t + 60 * m)], t, t + 30 * m, True),  # касание слева
        ([(t, t + 30 * m)], t + 15 * m, t + 45 * m, False),  # пересечение
        ([(t - 60 * m, t - 30 * m), (t, t + 30 * m)], t + 10 * m, t + 20 * m, False),  # конфликт во втором
        ([(t - 60 * m, t - 30 * m), (t + 60 * m, t + 90 * m)], t, t + 30 * m, True),  # свободно между
    ]
    passed = 0
    for busy, start, end, expected in cases:
        try:
            passed += fn(busy, start, end) is expected
        except Exception:  # noqa: BLE001, S112
            continue
    return passed, len(cases)


T3_REFERENCE = re.search(r"```python\n(.*?)```", (PROMPTS / "T3-tests.md").read_text(encoding="utf-8"), re.S).group(1)
T3_MUTANTS = {
    "inverted-check": ("if date_from > date_to:", "if date_from >= date_to:"),
    "no-plus-one": ("(date_to - date_from).days + 1", "(date_to - date_from).days"),
    "rest-off-by-one": ("for step in range(rest)", "for step in range(rest + 1)"),
    "weekday-shift": ("(first - 1 + step) % 7 + 1", "(first + step) % 7 + 1"),
    "floor-hours": ("total_seconds() // 60", "total_seconds() // 3600 * 60"),
    "ignore-workdays": ("full_weeks * len(work_days)", "full_weeks * 7"),
}


def _run_tests(tests: str, implementation: str) -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "minutes.py").write_text(implementation, encoding="utf-8")
        Path(tmp, "test_minutes.py").write_text(tests, encoding="utf-8")
        result = subprocess.run(  # noqa: S603
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_minutes.py"],
            cwd=tmp,
            capture_output=True,
            check=False,
            timeout=120,
        )
        return result.returncode == 0


def score_t3(tests: str) -> tuple[str, int, int]:
    if not tests or not _run_tests(tests, T3_REFERENCE):
        return "тесты падают на правильной реализации", 0, len(T3_MUTANTS)
    killed = 0
    for old, new in T3_MUTANTS.values():
        assert old in T3_REFERENCE, old
        killed += not _run_tests(tests, T3_REFERENCE.replace(old, new))
    return "проходят на правильной реализации", killed, len(T3_MUTANTS)


def score(model: str) -> None:
    folder = ANSWERS / slug(model)
    t1 = score_t1(code_block((folder / "T1.md").read_text(encoding="utf-8")))
    t2 = score_t2(code_block((folder / "T2.md").read_text(encoding="utf-8")))
    t3 = score_t3(code_block((folder / "T3.md").read_text(encoding="utf-8")))
    result = {
        "T1 проверок СНИЛС пройдено": f"{t1[0]}/{t1[1]}",
        "T2 сценариев исправленного кода": f"{t2[0]}/{t2[1]}",
        "T3 тесты": t3[0],
        "T3 убито мутантов": f"{t3[1]}/{t3[2]}",
    }
    (folder / "score.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    {"ask": ask, "score": score}[sys.argv[1]](sys.argv[2])

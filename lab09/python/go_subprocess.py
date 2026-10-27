"""Задание 3: вызов скомпилированной Go-программы из Python через subprocess.

Данные передаются в Go через stdin в виде JSON, результат читается из stdout.
"""

from __future__ import annotations

import json
import subprocess
from typing import Sequence

from common import build_go, configure_stdout


class CalculatorError(RuntimeError):
    """Go-калькулятор вернул ошибку."""


def calculate(numbers: Sequence[int], timeout: float = 10.0) -> dict:
    """Отправляет числа Go-калькулятору и возвращает словарь со статистикой."""
    binary = build_go("calculator")
    proc = subprocess.run(
        [str(binary)],
        input=json.dumps({"numbers": list(numbers)}),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
    )

    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise CalculatorError(
            f"некорректный ответ (код {proc.returncode}): {proc.stdout!r} {proc.stderr!r}"
        ) from exc

    if proc.returncode != 0 or "error" in payload:
        raise CalculatorError(payload.get("error", f"код выхода {proc.returncode}"))
    return payload


def main() -> None:
    configure_stdout()
    numbers = [1, 2, 3, 4, 5]
    print(f"Python -> Go: {numbers}")
    print("Go -> Python:", calculate(numbers))

    try:
        calculate([])
    except CalculatorError as exc:
        print("Ошибка обработана корректно:", exc)


if __name__ == "__main__":
    main()
